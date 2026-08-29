# -*- coding: utf-8 -*-
"""
Product model - extends product.template for cannabis products.

Odoo is a mirror of the Postgres `inventory` table in the canonical
pipeline (see docs/ARCHITECTURE.md in the frontend repo):
    Dutchie -> Postgres -> (Redis ^ Odoo) -> Frontend
Admins use this model as the authoring UI. Fields here must map 1:1
with the Dutchie-shape emitted to Redis; see the Shape Conformance
mapping table in ARCHITECTURE.md before adding or renaming a field.
"""
import re

from odoo import api, fields, models

# The tenant the LEGACY single-value `dutchie_brand_id` column was populated
# from, back when only AZ existed. Deliberately a literal and NOT resolved via
# res.company._dutchie_lsp(): this is a statement about where historical data
# came from, not a configurable mapping. If AZ were ever re-tenanted, the old
# column would still hold 575-era ids, so resolving it dynamically would be
# wrong. Per-tenant ids belong in `dutchie_brand_ids` ('lsp:id' lines).
LEGACY_BRAND_ID_SOURCE_LSP = 575


class ProductTemplate(models.Model):
    _inherit = "product.template"

    # Cannabis-specific fields
    is_cannabis = fields.Boolean(string="Is Cannabis Product", default=False)

    # `strain` is the legacy free-text column and is NOT deprecated: the
    # inventory service merges it into the Redis payload (cacheSync.js) and the
    # storefront types it (redis-api.ts), so it stays as the denormalized
    # mirror. `strain_id` is the authoring field — picking a master keeps the
    # text in sync automatically (see _sync_strain_vals).
    #
    # ⚠ This column is ALSO pre-created on every boot by fix-config.sh, before
    # Odoo starts. That is deliberate, not redundant: a stored field on
    # product.template whose column is missing breaks every write to the model
    # via ORM prefetch, and a rolled-back upgrade takes a migrations/ ALTER
    # down with it. Do not remove that block while this field exists.
    strain = fields.Char(string="Strain Name")
    strain_id = fields.Many2one(
        'mint.strain',
        string="Strain",
        index=True,
        ondelete='set null',
        help="Curated strain from the strain master. Setting this rewrites the "
             "free-text Strain Name to match; leaving it empty keeps whatever "
             "text the importer wrote.",
    )
    strain_type = fields.Selection([
        ('sativa', 'Sativa'),
        ('indica', 'Indica'),
        ('hybrid', 'Hybrid'),
        ('cbd', 'CBD'),
    ], string="Strain Type")

    # Potency
    thc_percentage = fields.Float(string="THC %", digits=(5, 2))
    cbd_percentage = fields.Float(string="CBD %", digits=(5, 2))
    thc_mg = fields.Float(string="THC (mg)")
    cbd_mg = fields.Float(string="CBD (mg)")
    # Raw potency strings as Dutchie returns them (e.g. "102.6 mg", "24.5%",
    # "n.d.", ""). The numeric Floats above are derived/parsed when meaningful;
    # these Chars are the source-of-truth from the POS feed.
    thc = fields.Char(string="THC (raw)", help="Raw potency string from Dutchie; parse later if a numeric value is needed.")
    cbd = fields.Char(string="CBD (raw)", help="Raw potency string from Dutchie; parse later if a numeric value is needed.")

    # Pricing
    rec_price = fields.Float(string="Recreational Price")
    med_price = fields.Float(string="Medical Price")

    # ── Categorization ───────────────────────────────────────────────────────
    # Three layers that are deliberately NOT the same axis. Collapsing them is
    # what produced the drift this replaces.
    #
    #   1. CONSUMER FORM — `master_category`. What a shopper filters by (flower
    #      / vapes / edibles …). Populated by RESOLVING Dutchie's taxonomies,
    #      never by copying one of them: use
    #      mint_inventory_ops.models.dutchie_receive_line.master_category_from_dutchie,
    #      whose three tiers (EcomCategory → POS MasterCategory → POS Category)
    #      cover 100% of consumer-facing product and return False rather than
    #      guess. 🚨 Its key list is duplicated as MASTER_CATEGORIES in that
    #      module — change both or receive lines and products disagree.
    #
    #   2. SOURCE MIRROR — the pos_/sub_/ecom_ Chars below. Dutchie's own
    #      values, verbatim and unnormalized. Char, not Selection, because the
    #      vocabulary is per-tenant and NOT congruent: measured live
    #      2026-08-27, FL runs a state MIP scheme ("MIP - Edibles",
    #      "Vaporization", "Gear") and MI treats "Prerolls" and "Accessories"
    #      as masters, while AZ/NV/IL/MO use the operational set in layer 3. A
    #      Selection silently drops whatever it does not enumerate — that is
    #      how the old mapper lost Bulk, Cultivation and Kitchen. Never
    #      normalize on the way in.
    #
    #   3. OPERATIONAL — `mint_ops_category`. Inventory/merchandising buckets
    #      per Natalie's "{ST} Dutchie Reference 2026" sheets: eight masters
    #      shared by all four sheets, plus Kitchen (AZ+NV) and Samples (FL+IL,
    #      where AZ/NV instead use "Samples (C/E/F/V/M/U)" sub-categories).
    master_category = fields.Selection([
        ('flower', 'Flower'),
        ('vaporizers', 'Vaporizers'),
        ('concentrates', 'Concentrates'),
        ('edibles', 'Edibles'),
        ('tinctures', 'Tinctures'),
        ('topicals', 'Topicals'),
        ('accessories', 'Accessories'),
        ('prerolls', 'Pre-Rolls'),
        ('beverages', 'Beverages'),
    ], string="Master Category",
       help="Consumer-facing product form. Resolved from Dutchie, not copied from it.")

    pos_master_category = fields.Char(
        string="Dutchie Master Category (POS)",
        help="Dutchie MasterCategory, mirrored verbatim. Per-tenant vocabulary — do not normalize.",
    )
    sub_category = fields.Char(
        string="Dutchie Sub-Category",
        help="Dutchie's POS Category field, mirrored verbatim — this is the vocabulary "
             "the state Dutchie Reference sheets call 'Sub-Category' (Prepack Flower, "
             "Cartridge: Live Resin, Infused Prerolls). Verified against 6,618 live rows "
             "2026-08-28: 116 distinct values carrying 32 of the 56 governance "
             "sub-categories. NOT Dutchie's `sub_category`, which is its ecom "
             "subcategory and duplicates ecom_subcategory exactly.",
    )
    ecom_category = fields.Char(
        string="Dutchie Ecom Category",
        help="Dutchie EcomCategory — the most cross-market-congruent taxonomy available "
             "(seven values present in all six markets). May also carry Dutchie's "
             "visibility flags 'Hide' / 'N/A', which are not categories.",
    )
    ecom_subcategory = fields.Char(
        string="Dutchie Ecom Sub-Category",
        help="Dutchie EcomSubcategory, mirrored verbatim.",
    )

    mint_sub_category = fields.Char(
        string="Mint Sub-Category",
        help="Governance sub-category — sub_category with the two mechanical drifts "
             "normalized away: singular/plural ('Preroll' vs 'Prerolls') and word order "
             "('Distillate Disposable' vs 'Disposables: Distillate'). 855 live rows are "
             "affected (MO 357, MI 273, IL 213). An allow-list, never a guess: a value "
             "with no governance equivalent passes through unchanged. Compare across "
             "states on this; audit against the verbatim sub_category.",
    )

    mint_ops_category = fields.Selection([
        ('flower', 'Flower'),
        ('vape', 'Vape'),
        ('concentrate', 'Concentrate'),
        ('edible', 'Edible'),
        ('medicated', 'Medicated'),
        ('unmedicated', 'Unmedicated'),
        ('bulk', 'Bulk'),
        ('cultivation', 'Cultivation'),
        ('kitchen', 'Kitchen'),
        ('samples', 'Samples'),
    ], string="Mint Operational Category",
       help="Inventory/merchandising master per the state Dutchie Reference sheets. A "
            "different axis from Master Category — Bulk, Cultivation, Kitchen and Samples "
            "are operational buckets, not consumer forms.")

    # Brand
    brand_id = fields.Many2one('mint.brand', string="Brand")

    def _sync_strain_vals(self, vals):
        """Keep `strain` (text) and `strain_id` (master) consistent on write.

        Two directions, because both are written by real callers:
          - a human picks `strain_id` on the form -> rewrite `strain` from it,
            so the Redis/storefront payload matches what the admin sees;
          - the Dutchie importer writes `strain` text -> resolve it to a master
            and set `strain_id`, so imported rows join the curated list without
            anyone re-keying them.

        An explicit value always wins: if the caller passes both, neither is
        overwritten. Mutates and returns `vals`.

        Deliberately tolerant of a missing mint.strain: if that model is not in
        the registry this is a no-op rather than an exception. product.template
        is written on every importer batch, and a strain lookup must never be
        able to take those writes down.
        """
        Strain = self.env.get('mint.strain')
        if Strain is None:
            return vals
        Strain = Strain.sudo()

        if vals.get('strain_id') and 'strain' not in vals:
            master = Strain.browse(vals['strain_id'])
            if master.exists():
                vals['strain'] = master.name
                if master.strain_type and 'strain_type' not in vals:
                    vals['strain_type'] = master.strain_type

        elif 'strain' in vals and 'strain_id' not in vals:
            # Resolve text -> master. Unresolvable text (a brand-new strain, or
            # a placeholder like "No Strain") clears the link rather than
            # leaving a stale one pointing at the previous strain.
            vals['strain_id'] = Strain.resolve_name(vals['strain']).id or False

        return vals

    def write(self, vals):
        vals = self._sync_strain_vals(dict(vals))

        old_brands = self.env['mint.brand']
        old_strains = None
        if 'brand_id' in vals or 'x_is_cannabis' in vals:
            old_brands = self.mapped('brand_id')
        if 'strain_id' in vals:
            old_strains = self.mapped('strain_id')

        res = super().write(vals)

        if 'brand_id' in vals or 'x_is_cannabis' in vals:
            affected = (old_brands | self.mapped('brand_id'))
            if affected:
                affected._compute_product_count()
        if old_strains is not None:
            affected = (old_strains | self.mapped('strain_id'))
            if affected:
                affected._refresh_counts_for()
        return res

    @api.model_create_multi
    def create(self, vals_list):
        vals_list = [self._sync_strain_vals(dict(v)) for v in vals_list]
        records = super().create(vals_list)
        affected = records.mapped('brand_id')
        if affected:
            affected._compute_product_count()
        affected_strains = records.mapped('strain_id')
        if affected_strains:
            affected_strains._refresh_counts_for()
        return records

    @api.onchange('strain_id')
    def _onchange_strain_id(self):
        """Mirror the picked master into the text + type fields in the form."""
        for record in self:
            if record.strain_id:
                record.strain = record.strain_id.name
                if record.strain_id.strain_type:
                    record.strain_type = record.strain_id.strain_type

    # Dutchie integration
    dutchie_product_id = fields.Char(string="Dutchie Product ID")
    dutchie_inventory_id = fields.Char(string="Dutchie Inventory ID")

    # Content
    effects = fields.Char(string="Effects (comma-separated)")
    flavors = fields.Char(string="Flavors (comma-separated)")
    tags = fields.Char(string="Tags (comma-separated)")
    staff_pick = fields.Boolean(string="Staff Pick", default=False)

    # Loyalty redemption: admins flag specific products as rewards + set cost.
    # When flagged, the product shows on /rewards and can be redeemed for points.
    x_is_loyalty_redeemable = fields.Boolean(
        string="Redeemable with Points",
        default=False,
        help="When enabled, customers can redeem this product using loyalty points on /rewards.",
    )
    x_loyalty_points_cost = fields.Integer(
        string="Points Cost",
        default=0,
        help="How many loyalty points this product costs to redeem.",
    )

    # Sync tracking
    synced_at = fields.Datetime(string="Last Synced")
    x_dutchie_modified_at = fields.Datetime(
        string="Dutchie Last Modified",
        help="Upstream Dutchie last-modified timestamp; distinct from native write_date which tracks our own writes.",
    )

    # ── Dutchie x_* fields (added 2026-04-16 per dutchie-to-odoo-worksheet) ─
    # These fields preserve Dutchie POS/inventory data that has no native Odoo
    # equivalent. Population thresholds documented in the worksheet; only fields
    # populated on >100 products nationally are stored.

    # Cannabis precision (state purchase-limit compliance)
    x_flower_equivalent       = fields.Float(string="Flower Equivalent",       help="Cannabis-equivalency metric used for state purchase limits (e.g. AZ 1oz). 95% populated.")
    x_rec_flower_equivalent   = fields.Float(string="Rec Flower Equivalent",   help="Recreational flower-equivalency variant.")
    x_flower_equivalent_units = fields.Char(string="Flower Equivalent Units",  help="Unit of flower equivalency (typically 'g').")
    x_effective_potency_mg    = fields.Float(string="Effective Potency (mg)",  help="Total mg of dominant cannabinoid (THC or CBD).")

    # Compliance / lab dates
    x_packaged_date      = fields.Date(string="Packaged Date",      help="Date the product was packaged.")
    x_manufacturing_date = fields.Date(string="Manufacturing Date", help="Date the product was manufactured. Native manufacturing_date lives on stock.lot only; this is the template-level mirror.")
    x_sample_date        = fields.Date(string="Sample Date",        help="Date sampled for lab testing.")
    x_tested_date        = fields.Date(string="Tested Date",        help="Lab test completion date.")
    x_expiration_date    = fields.Date(string="Expiration Date",    help="Absolute expiration date. Native expiration_time is Int days from manufacturing — different concept.")
    x_lab_test_status    = fields.Char(string="Lab Test Status",    help="Status string from Dutchie (e.g. 'Passed', 'Pending').")

    # Batch / package metadata (Dutchie-side identifiers; native stock.lot
    # integration is a separate Phase 2 ticket)
    x_batch_id            = fields.Char(string="Batch ID",            help="Dutchie batch identifier; future stock.lot integration is a separate ticket.")
    x_batch_name          = fields.Char(string="Batch Name",          help="Dutchie batch label.")
    x_package_id          = fields.Char(string="Package ID",          help="Dutchie package identifier; future stock.quant.package_id integration is a separate ticket.")
    x_package_status      = fields.Char(string="Package Status",      help="Dutchie package status; sparsely populated (~107/56k).")
    x_external_package_id = fields.Char(string="External Package ID", help="Dutchie external system package ID.")

    # Identity / merch metadata
    x_alternate_name    = fields.Char(string="Alternate Name",     help="Display alternate name.")
    x_dutchie_slug      = fields.Char(string="Dutchie Slug",       help="Dutchie URL slug — preserves cross-system linking. Native website_url is auto-computed.")
    x_size              = fields.Char(string="Size",               help="Size descriptor (e.g. '3.5g'). Often derivable from product name.")
    x_pricing_tier_name = fields.Char(string="Pricing Tier Name",  help="Tier label from Dutchie; lighter than a full product.pricelist integration.")

    # Producer (raw Dutchie payload — partner upsert is a separate ticket)
    x_producer_id   = fields.Char(string="Producer ID",   help="Dutchie producer identifier.")
    x_producer_json = fields.Text(string="Producer JSON", help="Raw producer JSON dump from Dutchie.")

    # Inventory units
    x_unit_weight      = fields.Float(string="Unit Weight",       help="Per-unit weight as Dutchie reports it. (Native `weight` would require uom conversion; x_* avoids unit-semantic ambiguity.)")
    x_unit_weight_unit = fields.Char(string="Unit Weight Unit",   help="Unit string for x_unit_weight (e.g. 'g', 'oz').")
    x_quantity_units   = fields.Char(string="Quantity Units",     help="Unit for quantity_available (e.g. 'qty', 'g').")

    @property
    def potency_thc_formatted(self):
        """Return formatted THC potency string."""
        if self.thc_mg:
            return f"{self.thc_mg}mg"
        elif self.thc_percentage:
            return f"{self.thc_percentage}%"
        return None

    @property
    def potency_cbd_formatted(self):
        """Return formatted CBD potency string."""
        if self.cbd_mg:
            return f"{self.cbd_mg}mg"
        elif self.cbd_percentage:
            return f"{self.cbd_percentage}%"
        return None

    def get_effects_list(self):
        """Return effects as a list."""
        if not self.effects:
            return []
        return [e.strip() for e in self.effects.split(',') if e.strip()]

    def get_tags_list(self):
        """Return tags as a list."""
        if not self.tags:
            return []
        return [t.strip() for t in self.tags.split(',') if t.strip()]


class MintBrand(models.Model):
    _name = "mint.brand"
    _description = "Cannabis Brand"

    name = fields.Char(string="Name", required=True)
    slug = fields.Char(string="URL Slug")
    logo = fields.Binary(string="Logo")
    logo_url = fields.Char(string="Logo URL")
    description = fields.Text(string="Description")
    website = fields.Char(string="Website")
    dutchie_brand_id = fields.Char(
        string="Dutchie Brand ID",
        index=True,
        help="Upstream Dutchie BrandId, used to resolve discount brand restrictions to Odoo brands at sync time.",
    )
    dutchie_brand_ids = fields.Text(
        string="Dutchie Brand IDs (per LSP)",
        help="Per-LSP Dutchie BrandIds, one 'lsp:id' per line (e.g. '575:27901').\n"
             "Dutchie BrandIds are tenant-scoped — a brand has a DIFFERENT id in\n"
             "each LSP (575 AZ, 576 MI, 723 MO, 805 IL, 820 NV, 821 FL) — so a\n"
             "single id cannot target a brand across states. dutchie_brand_id\n"
             "stays the AZ/legacy value for back-compat; this is the source of\n"
             "truth for cross-state discount publishing.",
    )

    aliases = fields.Text(
        string="Aliases",
        help="Alternate vendor-facing names for this brand, one per line "
             "(e.g. 'BackpackBoyz' for 'Backpack Boyz', 'GTI brands' for "
             "'Green Thumb'). Used by resolve_vendor_string() to match the "
             "free-text brand names vendors type into deal submissions.",
    )

    @staticmethod
    def _norm_brand_name(s):
        """Normalize a brand string for matching: lowercase, '&'->'and',
        strip punctuation and filler tokens. Mirrors the JS importer's norm()."""
        s = (s or '').lower().replace('&', ' and ')
        s = re.sub(r'[^a-z0-9]+', ' ', s)
        s = re.sub(r'\b(the|brands?|inc|llc|co|cannabis|edibles|vapes)\b', ' ', s)
        return re.sub(r'\s+', ' ', s).strip()

    @api.model
    def resolve_vendor_string(self, vendor_string):
        """Resolve a vendor-typed brand string to mint.brand records.

        Handles multi-brand strings by splitting on '/', '&', '+', ',', '·'
        and ' and ', then matching each part against brand names AND alias
        lines (normalized both sides). Returns a recordset — possibly several
        brands for 'Alien Labs & Connected', possibly empty.
        """
        if not vendor_string:
            return self.browse()
        index = {}
        for b in self.search_read([], ['name', 'aliases']):
            n = self._norm_brand_name(b['name'])
            if n and n not in index:
                index[n] = b['id']
            for line in (b['aliases'] or '').splitlines():
                a = self._norm_brand_name(line)
                if a and a not in index:
                    index[a] = b['id']

        whole = index.get(self._norm_brand_name(vendor_string))
        if whole:
            return self.browse(whole)
        ids = []
        for part in re.split(r'[/&+,·]|\band\b', vendor_string, flags=re.I):
            hit = index.get(self._norm_brand_name(part))
            if hit and hit not in ids:
                ids.append(hit)
        return self.browse(ids)

    def dutchie_brand_id_for_lsp(self, lsp_id):
        """Return this brand's Dutchie BrandId for the given LSP, or False.

        Reads the 'lsp:id' lines in dutchie_brand_ids; falls back to the legacy
        single-value dutchie_brand_id only when the target is the tenant that
        column was populated from. Use this when building a Dutchie discount
        payload so the Brand restriction carries the id for the target tenant.
        """
        self.ensure_one()
        target = str(lsp_id).strip()
        for line in (self.dutchie_brand_ids or '').splitlines():
            line = line.strip()
            if not line or ':' not in line:
                continue
            lsp, _, bid = line.partition(':')
            if lsp.strip() == target and bid.strip():
                return bid.strip()
        if target == str(LEGACY_BRAND_ID_SOURCE_LSP) and self.dutchie_brand_id:
            return str(self.dutchie_brand_id).strip()
        return False

    # Gate for discount-targeting dropdowns: only brands with actual cannabis
    # products appear in domain-filtered Many2many pickers. Stored because
    # domains can only filter on stored fields. Recomputed on module upgrade
    # and by nightly cron; product.template writes also trigger via inverse.
    product_count = fields.Integer(
        string="# Cannabis Products",
        compute='_compute_product_count',
        store=True,
        help="Count of cannabis product.template records linked to this brand.",
    )

    @api.depends('name')  # trivial dep — real recompute via _sync_brand_product_count below
    def _compute_product_count(self):
        Product = self.env['product.template'].sudo()
        for brand in self:
            brand.product_count = Product.search_count([
                ('brand_id', '=', brand.id),
                ('x_is_cannabis', '=', True),
            ])


class ProductCategory(models.Model):
    _inherit = "product.category"

    dutchie_category_id = fields.Char(
        string="Dutchie Category ID",
        index=True,
        help="Upstream Dutchie CategoryId, used to resolve discount category restrictions to Odoo categories at sync time.",
    )
