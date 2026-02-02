# -*- coding: utf-8 -*-
"""
Discount model for cannabis deals and promotions.
"""
from odoo import api, fields, models
from datetime import date


class MintDiscount(models.Model):
    _name = "mint.discount"
    _description = "Cannabis Discount/Deal"
    _order = "valid_from desc, id desc"

    name = fields.Char(string="Name", required=True)
    slug = fields.Char(string="URL Slug", index=True)
    code = fields.Char(string="Discount Code")
    description = fields.Text(string="Description")
    terms = fields.Text(string="Terms & Conditions")

    # Discount configuration
    discount_type = fields.Selection([
        ('percent', 'Percentage Off'),
        ('fixed', 'Fixed Amount Off'),
        ('price_to_amount', 'Price To Amount'),
        ('bogo', 'Buy One Get One'),
    ], string="Discount Type", required=True, default='percent')
    discount_amount = fields.Float(string="Discount Amount")
    discount_percent = fields.Float(string="Discount Percentage")

    # Status
    is_active = fields.Boolean(string="Active", default=True)
    is_featured = fields.Boolean(string="Featured", default=False)
    is_available_online = fields.Boolean(string="Available Online", default=True)

    # Validity
    valid_from = fields.Date(string="Valid From", required=True, default=fields.Date.today)
    valid_until = fields.Date(string="Valid Until")

    # Targeting - Stores (multiple stores)
    store_ids = fields.Many2many(
        'res.company',
        'mint_discount_store_rel',
        'discount_id',
        'store_id',
        string="Stores",
        domain=[('is_dispensary', '=', True)]
    )

    # Targeting - Products
    product_ids = fields.Many2many(
        'product.template',
        'mint_discount_product_rel',
        'discount_id',
        'product_id',
        string="Products"
    )
    exclude_product_ids = fields.Many2many(
        'product.template',
        'mint_discount_exclude_product_rel',
        'discount_id',
        'product_id',
        string="Excluded Products"
    )

    # Targeting - Categories
    category_ids = fields.Many2many(
        'product.category',
        'mint_discount_category_rel',
        'discount_id',
        'category_id',
        string="Categories"
    )
    exclude_category_ids = fields.Many2many(
        'product.category',
        'mint_discount_exclude_category_rel',
        'discount_id',
        'category_id',
        string="Excluded Categories"
    )

    # Targeting - Brands
    brand_ids = fields.Many2many(
        'mint.brand',
        'mint_discount_brand_rel',
        'discount_id',
        'brand_id',
        string="Brands"
    )

    # Eligibility rules
    threshold_type = fields.Selection([
        ('none', 'No Threshold'),
        ('items', 'Number of Items'),
        ('order_total', 'Order Total'),
        ('subtotal', 'Subtotal'),
    ], string="Threshold Type", default='none')
    threshold_min = fields.Float(string="Minimum Threshold")
    threshold_max = fields.Float(string="Maximum Threshold")
    minimum_items_required = fields.Integer(string="Minimum Items Required", default=1)
    first_time_customer_only = fields.Boolean(string="First Time Customers Only", default=False)

    # Bundling
    is_bundled_discount = fields.Boolean(string="Is Bundled Discount", default=False)

    # Media
    image = fields.Binary(string="Discount Image")
    image_url = fields.Char(string="Discount Image URL")

    # External integration
    dutchie_discount_id = fields.Char(string="Dutchie Discount ID")

    # Sync tracking
    synced_at = fields.Datetime(string="Last Synced")

    @api.model
    def get_active_discounts(self, store_id=None):
        """Return active discounts, optionally filtered by store."""
        domain = [
            ('is_active', '=', True),
            '|',
            ('valid_until', '=', False),
            ('valid_until', '>=', date.today()),
            ('valid_from', '<=', date.today()),
        ]
        if store_id:
            domain.append(('store_ids', 'in', [store_id]))
        return self.search(domain)

    def applies_to_product(self, product):
        """Check if this discount applies to a given product."""
        self.ensure_one()

        # Check exclusions first
        if product.id in self.exclude_product_ids.ids:
            return False
        if product.categ_id.id in self.exclude_category_ids.ids:
            return False

        # Check inclusions
        if self.product_ids and product.id not in self.product_ids.ids:
            return False
        if self.category_ids and product.categ_id.id not in self.category_ids.ids:
            return False
        if self.brand_ids and product.brand_id.id not in self.brand_ids.ids:
            return False

        return True
