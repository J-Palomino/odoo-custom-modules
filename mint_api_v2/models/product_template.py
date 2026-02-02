# -*- coding: utf-8 -*-
"""
Product model - extends product.template for cannabis products.
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
