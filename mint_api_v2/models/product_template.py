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

    # Dutchie integration
    dutchie_product_id = fields.Char(string="Dutchie Product ID")
    dutchie_inventory_id = fields.Char(string="Dutchie Inventory ID")

    # Content
    effects = fields.Char(string="Effects (comma-separated)")
    flavors = fields.Char(string="Flavors (comma-separated)")
    tags = fields.Char(string="Tags (comma-separated)")
    staff_pick = fields.Boolean(string="Staff Pick", default=False)

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


class ProductCategory(models.Model):
    _inherit = "product.category"

    dutchie_category_id = fields.Char(
        string="Dutchie Category ID",
        index=True,
        help="Upstream Dutchie CategoryId, used to resolve discount category restrictions to Odoo categories at sync time.",
    )
