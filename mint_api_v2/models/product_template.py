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
from odoo import api, fields, models


class ProductTemplate(models.Model):
    _inherit = "product.template"

    # Cannabis-specific fields
    is_cannabis = fields.Boolean(string="Is Cannabis Product", default=False)
    strain = fields.Char(string="Strain Name")
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

    # Categorization
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
    ], string="Master Category")

    # Brand
    brand_id = fields.Many2one('mint.brand', string="Brand")

    def write(self, vals):
        old_brands = self.env['mint.brand']
        if 'brand_id' in vals or 'x_is_cannabis' in vals:
            old_brands = self.mapped('brand_id')
        res = super().write(vals)
        if 'brand_id' in vals or 'x_is_cannabis' in vals:
            affected = (old_brands | self.mapped('brand_id'))
            if affected:
                affected._compute_product_count()
        return res

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        affected = records.mapped('brand_id')
        if affected:
            affected._compute_product_count()
        return records

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

    def dutchie_brand_id_for_lsp(self, lsp_id):
        """Return this brand's Dutchie BrandId for the given LSP, or False.

        Reads the 'lsp:id' lines in dutchie_brand_ids; falls back to the legacy
        dutchie_brand_id only for AZ (LSP 575, which is what it was populated
        from). Use this when building a Dutchie discount payload so the
        Brand restriction carries the id for the discount's target tenant.
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
        if target == '575' and self.dutchie_brand_id:
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
