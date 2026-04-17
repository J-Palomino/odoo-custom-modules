# -*- coding: utf-8 -*-
"""
Discount model for cannabis deals and promotions.

Odoo mirror of the Postgres `discounts` table. Canonical pipeline
(see docs/ARCHITECTURE.md in the frontend repo):
    Dutchie -> Postgres -> (Redis ^ Odoo) -> Frontend
PTL (marketing-authored) discounts originate here with source='ptl'
and flow back to PG via webhook. Dutchie POS discounts originate in
PG with source='dutchie' and are mirrored here. Fields must map 1:1
with the Dutchie-shape emitted to Redis; see the Shape Conformance
mapping table in ARCHITECTURE.md before adding or renaming a field.
"""
import secrets

from odoo import api, fields, models, _
from odoo.exceptions import UserError
from datetime import date, datetime, timedelta


# Loyalty redemption code: default 30-day expiry, prefixed for visual grouping.
REDEMPTION_CODE_BYTES = 5   # 10 hex chars → ~1T space
REDEMPTION_DEFAULT_TTL_DAYS = 30


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
        ('points_multiplier', 'Loyalty Points Multiplier'),
        ('loyalty_redemption', 'Loyalty Redemption'),
        ('clearance', 'Clearance (Near Expiry)'),
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
    excluded_skus = fields.Text(
        string="Excluded SKUs",
        help="Newline- or comma-separated SKUs to exclude from this discount. "
             "Matched case-insensitively against product default_code.",
    )

    # Targeting - Brands
    brand_ids = fields.Many2many(
        'mint.brand',
        'mint_discount_brand_rel',
        'discount_id',
        'brand_id',
        string="Brands"
    )
    exclude_brand_ids = fields.Many2many(
        'mint.brand',
        'mint_discount_exclude_brand_rel',
        'discount_id',
        'brand_id',
        string="Excluded Brands",
        help="Products from these brands are excluded from this discount.",
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
    stack_on_other_discounts = fields.Boolean(
        string="Stack With Other Discounts", default=False,
        help="Mirrors Dutchie `stack_on_other_discounts` — whether this deal combines with others on the same cart line.",
    )

    # Media
    image = fields.Binary(string="Discount Image")
    image_url = fields.Char(string="Discount Image URL")

    # External integration
    source = fields.Selection(
        [('ptl', 'Push-to-Live (Odoo-authored)'),
         ('dutchie', 'Dutchie POS (mirrored)')],
        string="Source", default='ptl', index=True,
        help="Origin system. 'ptl' = authored here and pushed out; 'dutchie' = mirrored in from Dutchie POS.",
    )
    calculation_method = fields.Char(
        string="Calculation Method",
        help="Raw Dutchie calculation method string (PERCENT_OFF, FIXED_AMOUNT_OFF, "
             "PRICE_TO_AMOUNT, PRICE_TO_AMOUNT_TOTAL, BOGO). Preserves the exact "
             "semantics so bundle deals (TOTAL) stay distinct from per-unit price-to-amount.",
    )
    dutchie_discount_id = fields.Char(string="Dutchie Discount ID", index=True)
    dutchie_external_id = fields.Char(
        string="Dutchie External ID",
        help="Dutchie's own `external_id` on the discount record (distinct from our "
             "dutchie_discount_id which is Dutchie's internal numeric id).",
    )

    # Day-of-week validity (mirrors Dutchie fields; default True = runs every day).
    # When a Dutchie deal is restricted (e.g. Tuesdays only), the non-matching
    # days come in as False and the discount is skipped on those days.
    monday    = fields.Boolean(default=True)
    tuesday   = fields.Boolean(default=True)
    wednesday = fields.Boolean(default=True)
    thursday  = fields.Boolean(default=True)
    friday    = fields.Boolean(default=True)
    saturday  = fields.Boolean(default=True)
    sunday    = fields.Boolean(default=True)

    # Sync tracking
    synced_at = fields.Datetime(string="Last Synced")

    # ── Loyalty Redemption (discount_type='loyalty_redemption') ─────────
    # A redemption is a single-use discount handed to one customer after
    # they spent points on /rewards. Budtender marks it used in-store.
    redemption_code = fields.Char(
        string="Redemption Code", index=True, copy=False,
        help="Short code shown to the customer and scanned/typed in-store.",
    )
    redemption_partner_id = fields.Many2one(
        'res.partner', string="Redeeming Customer", ondelete='restrict',
    )
    redemption_reward_id = fields.Many2one(
        'loyalty.reward', string="Reward", ondelete='restrict',
    )
    redemption_product_id = fields.Many2one(
        'product.template', string="Reward Product", ondelete='restrict',
        help="The product the customer is redeeming (set when redemption is product-scoped).",
    )
    redemption_points_cost = fields.Integer(string="Points Cost")
    redemption_status = fields.Selection([
        ('pending', 'Pending'),
        ('used', 'Used'),
        ('expired', 'Expired'),
        ('voided', 'Voided'),
    ], string="Redemption Status", default='pending', copy=False)
    redemption_used_at = fields.Datetime(string="Used At", readonly=True, copy=False)
    redemption_used_by_id = fields.Many2one(
        'res.users', string="Used By", readonly=True, copy=False,
        help="Budtender or staff member who marked this redemption used.",
    )
    expires_at = fields.Datetime(string="Expires At", copy=False)

    _sql_constraints = [
        ('redemption_code_unique',
         'UNIQUE(redemption_code)',
         'Redemption code must be unique.'),
    ]

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

    def _excluded_sku_set(self):
        """Parse excluded_skus into a normalized set of uppercase SKU strings."""
        self.ensure_one()
        if not self.excluded_skus:
            return set()
        raw = self.excluded_skus.replace(',', '\n')
        return {tok.strip().upper() for tok in raw.split('\n') if tok.strip()}

    def applies_to_product(self, product):
        """Check if this discount applies to a given product."""
        self.ensure_one()

        # Check exclusions first
        if product.id in self.exclude_product_ids.ids:
            return False
        if product.categ_id.id in self.exclude_category_ids.ids:
            return False
        excluded_skus = self._excluded_sku_set()
        if excluded_skus and product.default_code and product.default_code.upper() in excluded_skus:
            return False
        if self.exclude_brand_ids and product.brand_id.id in self.exclude_brand_ids.ids:
            return False

        # Check inclusions
        if self.product_ids and product.id not in self.product_ids.ids:
            return False
        if self.category_ids and product.categ_id.id not in self.category_ids.ids:
            return False
        if self.brand_ids and product.brand_id.id not in self.brand_ids.ids:
            return False

        return True

    # ── Loyalty Redemption API ─────────────────────────────────────────

    @api.model
    def _generate_redemption_code(self):
        """Return a short unique hex code (e.g. 'A7F3B92D01')."""
        for _ in range(8):
            candidate = secrets.token_hex(REDEMPTION_CODE_BYTES).upper()
            if not self.sudo().search_count([('redemption_code', '=', candidate)]):
                return candidate
        raise UserError(_("Could not generate a unique redemption code."))

    @api.model
    def create_redemption(self, partner, points_cost, reward=None, product=None):
        """Create a loyalty_redemption discount for a customer.

        Caller is responsible for deducting points from the loyalty.card
        inside the same transaction. Supply either `reward` (generic tier)
        or `product` (specific product.template). Returns the new record.
        """
        if not partner:
            raise UserError(_("Partner is required."))
        if not reward and not product:
            raise UserError(_("Either reward or product is required."))

        label = (product.name if product else None) or (reward.display_name if reward else _("Reward"))
        expires = fields.Datetime.now() + timedelta(days=REDEMPTION_DEFAULT_TTL_DAYS)
        return self.sudo().create({
            'name': _("Redemption: %s") % label,
            'discount_type': 'loyalty_redemption',
            'is_active': True,
            'is_available_online': False,
            'valid_from': fields.Date.today(),
            'valid_until': expires.date(),
            'redemption_code': self._generate_redemption_code(),
            'redemption_partner_id': partner.id,
            'redemption_reward_id': reward.id if reward else False,
            'redemption_product_id': product.id if product else False,
            'redemption_points_cost': points_cost,
            'redemption_status': 'pending',
            'expires_at': expires,
        })

    def action_mark_redemption_used(self):
        """Budtender action: mark this redemption as used."""
        for rec in self:
            if rec.discount_type != 'loyalty_redemption':
                raise UserError(_("Only loyalty redemptions can be marked used."))
            if rec.redemption_status != 'pending':
                raise UserError(_(
                    "Redemption %s is already %s.",
                    rec.redemption_code, rec.redemption_status,
                ))
            if rec.expires_at and rec.expires_at < fields.Datetime.now():
                rec.redemption_status = 'expired'
                raise UserError(_(
                    "Redemption %s expired on %s.",
                    rec.redemption_code, rec.expires_at,
                ))
            rec.write({
                'redemption_status': 'used',
                'redemption_used_at': fields.Datetime.now(),
                'redemption_used_by_id': self.env.user.id,
                'is_active': False,
            })
        return True

    def action_void_redemption(self):
        """Void a pending redemption and refund points to the customer."""
        LoyaltyCard = self.env['loyalty.card'].sudo()
        for rec in self:
            if rec.discount_type != 'loyalty_redemption':
                raise UserError(_("Only loyalty redemptions can be voided."))
            if rec.redemption_status != 'pending':
                raise UserError(_(
                    "Cannot void redemption %s (status: %s).",
                    rec.redemption_code, rec.redemption_status,
                ))
            reward = rec.redemption_reward_id
            if reward and rec.redemption_partner_id and rec.redemption_points_cost:
                card = LoyaltyCard.search([
                    ('partner_id', '=', rec.redemption_partner_id.id),
                    ('program_id', '=', reward.program_id.id),
                ], limit=1)
                if card:
                    card.points = card.points + rec.redemption_points_cost
            rec.write({
                'redemption_status': 'voided',
                'is_active': False,
            })
        return True

    @api.model
    def consume_pending_redemption(self, partner):
        """Auto-consume the oldest pending redemption for a partner.

        Called from order-completion paths (online checkout + Dutchie POS
        sync). Points were already deducted at redeem time, so this only
        flips status -> 'used' and stamps used_at. Returns the consumed
        record (browse of 1) or empty recordset.
        """
        if not partner:
            return self.browse()
        redemption = self.sudo().search([
            ('discount_type', '=', 'loyalty_redemption'),
            ('redemption_partner_id', '=', partner.id),
            ('redemption_status', '=', 'pending'),
        ], order='create_date asc', limit=1)
        if not redemption:
            return self.browse()
        # Expired? Sweep to expired and skip.
        if redemption.expires_at and redemption.expires_at < fields.Datetime.now():
            redemption.write({'redemption_status': 'expired', 'is_active': False})
            return self.browse()
        redemption.write({
            'redemption_status': 'used',
            'redemption_used_at': fields.Datetime.now(),
            'is_active': False,
        })
        return redemption

    @api.model
    def expire_pending_redemptions(self):
        """Cron: mark pending redemptions past expires_at as expired."""
        now = fields.Datetime.now()
        stale = self.sudo().search([
            ('discount_type', '=', 'loyalty_redemption'),
            ('redemption_status', '=', 'pending'),
            ('expires_at', '<', now),
        ])
        if stale:
            stale.write({'redemption_status': 'expired', 'is_active': False})
        return len(stale)
