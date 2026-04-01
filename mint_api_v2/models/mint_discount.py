# -*- coding: utf-8 -*-
"""
Discount model for cannabis deals and promotions.
"""
import json
import logging
import threading
import urllib.request
from datetime import date, timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


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

    # Deal classification (controls display on website /deals page)
    deal_classification = fields.Selection([
        ('sale', 'Sale'),
        ('daily', 'Daily Deal'),
        ('first_time', 'First-Time Customer'),
    ], string="Deal Classification", default='sale',
       help="Controls how this discount appears on the website deals page")

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

    # Day-of-week availability (computed from PTL day associations)
    monday = fields.Boolean(string="Monday", default=False)
    tuesday = fields.Boolean(string="Tuesday", default=False)
    wednesday = fields.Boolean(string="Wednesday", default=False)
    thursday = fields.Boolean(string="Thursday", default=False)
    friday = fields.Boolean(string="Friday", default=False)
    saturday = fields.Boolean(string="Saturday", default=False)
    sunday = fields.Boolean(string="Sunday", default=False)

    # Source tracking
    source = fields.Selection([
        ('ptl', 'PTL'),
        ('dutchie', 'Dutchie POS'),
        ('manual', 'Manual'),
    ], string="Source", default='manual')

    # Link to PTL deal template (if created from PTL)
    ptl_deal_id = fields.Many2one(
        'mint.ptl.deal',
        string="PTL Deal",
        ondelete='set null',
    )

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

    DAY_NAME_MAP = {
        0: 'monday', 1: 'tuesday', 2: 'wednesday', 3: 'thursday',
        4: 'friday', 5: 'saturday', 6: 'sunday',
    }

    @api.model
    def _cron_ptl_daily_lifecycle(self):
        """Daily lifecycle for PTL discounts: activate, expire, recompute days, push."""
        today = fields.Date.today()

        # Activate deals where valid_from <= today
        to_activate = self.search([
            ('source', '=', 'ptl'),
            ('is_active', '=', False),
            ('valid_from', '<=', today),
            '|', ('valid_until', '=', False), ('valid_until', '>=', today),
        ])
        if to_activate:
            to_activate.write({'is_active': True})
            _logger.info('PTL lifecycle: activated %d discounts', len(to_activate))

        # Expire deals where valid_until < today
        to_expire = self.search([
            ('source', '=', 'ptl'),
            ('is_active', '=', True),
            ('valid_until', '!=', False),
            ('valid_until', '<', today),
        ])
        if to_expire:
            to_expire.write({'is_active': False})
            _logger.info('PTL lifecycle: expired %d discounts', len(to_expire))

        # Recompute day-of-week booleans for active PTL discounts
        active_ptl = self.search([
            ('source', '=', 'ptl'),
            ('is_active', '=', True),
            ('ptl_deal_id', '!=', False),
        ])
        for discount in active_ptl:
            self._recompute_day_booleans(discount)

        # Push all changes to inventory service
        changed = to_activate | to_expire | active_ptl
        if changed:
            self.env['mint.ptl.day']._push_discounts_to_redis(changed.ids)

        _logger.info('PTL lifecycle cron complete: %d activated, %d expired, %d recomputed',
                      len(to_activate), len(to_expire), len(active_ptl))

    def _recompute_day_booleans(self, discount):
        """Recompute monday-sunday booleans from linked PTL day dates."""
        deal = discount.ptl_deal_id
        if not deal:
            return

        today = fields.Date.today()
        future_cutoff = today + timedelta(days=60)

        days = self.env['mint.ptl.day'].search([
            ('deal_ids', 'in', deal.id),
            ('date', '>=', today),
            ('date', '<=', future_cutoff),
        ])

        day_bools = {name: False for name in self.DAY_NAME_MAP.values()}
        for day_rec in days:
            weekday_num = day_rec.date.weekday()
            day_bools[self.DAY_NAME_MAP[weekday_num]] = True

        discount.write(day_bools)

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
