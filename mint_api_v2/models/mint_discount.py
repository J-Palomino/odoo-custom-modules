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
import json
import logging
import secrets
import urllib.request as urlrequest
from urllib.error import HTTPError
from urllib.parse import quote as url_quote

from odoo import api, fields, models, tools, _
from odoo.exceptions import UserError
from datetime import date, datetime, timedelta

_logger = logging.getLogger(__name__)


# Loyalty redemption code: default 30-day expiry, prefixed for visual grouping.
REDEMPTION_CODE_BYTES = 5   # 10 hex chars → ~1T space
REDEMPTION_DEFAULT_TTL_DAYS = 30
# valid_from is a Date and Dutchie evaluates ValidDateFrom in store wall clock,
# while the server date rolls over at midnight UTC (5pm AZ). A redemption
# minted in that evening window would start "tomorrow" and be refused at the
# register all night. Back off by a slack day, as the welcome pre-roll issuer
# does (WELCOME_VALID_FROM_SLACK_DAYS).
REDEMPTION_VALID_FROM_SLACK_DAYS = 1

# Cent tolerance for treating a line as "free" after per-line discount.
FREE_LINE_TOLERANCE = 0.01


def _find_matching_free_line(redemption, order_items):
    """Return the first order_items dict that matches redemption's product
    and is effectively free, or None. Matches on dutchie_product_id first,
    falls back to SKU (default_code). An item is free when
    ``unit_price * quantity - discount <= FREE_LINE_TOLERANCE``.

    Matches against every template in ``product_ids``, not only
    ``redemption_product_id``: create_redemption widens the scope to the
    sibling templates of the redeemed product (same item at other stores in
    the region, under that store's own Dutchie id/SKU), so an order at any of
    those stores must still consume the coupon.
    """
    products = redemption.product_ids or redemption.redemption_product_id
    if not products:
        return None
    prod_pids = {pid for pid in
                 ((p.dutchie_product_id or '').strip() for p in products) if pid}
    prod_skus = {sku for sku in
                 ((p.default_code or '').strip().upper() for p in products) if sku}

    for item in order_items or []:
        line_pid = str(item.get('dutchie_product_id') or '').strip()
        line_sku = str(item.get('sku') or '').strip().upper()
        id_match = line_pid in prod_pids or line_sku in prod_skus
        if not id_match:
            continue
        try:
            qty = float(item.get('quantity') or 1)
            unit = float(item.get('unit_price') or 0)
            disc = float(item.get('discount') or 0)
        except (TypeError, ValueError):
            continue
        if (unit * qty) - disc <= FREE_LINE_TOLERANCE:
            return item
    return None


class MintDiscount(models.Model):
    _name = "mint.discount"
    _description = "Cannabis Discount/Deal"
    _inherit = ["mail.thread", "mail.activity.mixin"]
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
    discount_percent = fields.Float(
        string="Discount Percentage",
        help="DEPRECATED — superseded by discount_amount + calculation_method_id. "
             "Not emitted to Redis or pushed to Dutchie; live data showed it was "
             "0.0 on all Dutchie-mirrored rows. Kept (not dropped) for back-compat. "
             "Storefront percent deals carry their fraction in discount_amount.",
    )

    # Status
    # `is_active` becomes a stored compute in mint_command_center (driven by
    # is_published + valid window + day-of-week + start/end_time). Definition
    # kept here so consumers in this module can still reference it.
    is_active = fields.Boolean(string="Active", default=True)
    is_published = fields.Boolean(
        string="Published",
        default=False,
        index=True,
        copy=False,
        help="Marketing-controlled flag. The computed is_active gate flows from this "
             "plus the valid window, day-of-week, and start/end time fields.",
    )
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

    # Targeting - Weights (Dutchie Reward.Restrictions.Weight)
    weight_ids = fields.Many2many(
        'mint.discount.weight',
        'mint_discount_weight_rel',
        'discount_id',
        'weight_id',
        string="Weights",
        help="Apply only to products matching these weight values (grams).",
    )
    exclude_weight_ids = fields.Many2many(
        'mint.discount.weight',
        'mint_discount_exclude_weight_rel',
        'discount_id',
        'weight_id',
        string="Excluded Weights",
        help="Products with these weights are excluded from this discount.",
    )

    # Targeting - Vendors (Dutchie Reward.Restrictions.Vendor)
    # Vendors are res.partner records linked via product.supplierinfo.
    vendor_ids = fields.Many2many(
        'res.partner',
        'mint_discount_vendor_rel',
        'discount_id',
        'partner_id',
        string="Vendors",
        domain="[('supplier_rank', '>', 0)]",
        help="Apply only to products supplied by these vendors.",
    )
    exclude_vendor_ids = fields.Many2many(
        'res.partner',
        'mint_discount_exclude_vendor_rel',
        'discount_id',
        'partner_id',
        string="Excluded Vendors",
        domain="[('supplier_rank', '>', 0)]",
        help="Products from these vendors are excluded from this discount.",
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
    maximum_items_allowed = fields.Integer(string="Maximum Items Allowed")
    maximum_usage_count = fields.Integer(string="Maximum Usage Count")
    first_time_customer_only = fields.Boolean(string="First Time Customers Only", default=False)
    include_non_cannabis = fields.Boolean(string="Include Non-Cannabis Items", default=False)

    # Dutchie application behavior
    application_method = fields.Selection([
        ('automatic', 'Automatic'),
        ('code', 'Code'),
        ('manual', 'Manual'),
    ], string="Application Method",
       help="How Dutchie applies this discount. Manual discounts are staff-applied (not customer-facing).")
    # The actual coupon code a budtender (or customer) types at the Dutchie
    # register when application_method='code'. Pushed verbatim as the Dutchie
    # `DiscountCode`. DISTINCT from `redemption_code` below, which is an internal
    # Odoo loyalty-redemption token and is never sent to Dutchie.
    dutchie_discount_code = fields.Char(
        string="Dutchie Discount Code", copy=False,
        help="Code typed at the Dutchie register (ApplicationMethodId=3). Pushed "
             "verbatim as the Dutchie DiscountCode. Leave blank for "
             "automatic/manual discounts.")
    require_manager_approval = fields.Boolean(string="Requires Manager Approval", default=False)
    stack_on_other_discounts = fields.Boolean(string="Stacks With Other Discounts", default=False)
    apply_to_only_one_item = fields.Boolean(string="Apply To Only One Item", default=False)

    # Time-of-day targeting (paired with day-of-week booleans)
    start_time = fields.Float(string="Start Time", help="Hour-of-day (0-24) when discount becomes active.")
    end_time = fields.Float(string="End Time", help="Hour-of-day (0-24) when discount stops.")

    # Bundling
    is_bundled_discount = fields.Boolean(string="Is Bundled Discount", default=False)

    # Media
    image = fields.Binary(string="Discount Image")
    image_url = fields.Char(string="Discount Image URL")

    # External integration
    # Note: `source` (Selection) and day-of-week bools (monday..sunday) are
    # defined on the _inherit extension in mint_command_center/
    # models/mint_discount_ext.py — don't redefine here or defaults collide.
    dutchie_discount_id = fields.Char(string="Dutchie Discount ID", index=True)
    online_name = fields.Char(string="Online Name", help="Customer-facing name from Dutchie `onlineName`.")
    menu_display_name = fields.Char(string="Menu Display Name", help="Dutchie menu card label.")

    # Sync tracking
    synced_at = fields.Datetime(string="Last Synced")

    # Raw Dutchie payload archive. Mirrors PG.discounts.raw_payload — the
    # full normalized item Dutchie returned at last sync. Anything outside
    # the explicit field mapping (new Dutchie fields, restriction types we
    # don't extract, constraint metadata) is recoverable from this blob
    # without re-fetching from Dutchie. Stored as Text (JSON-encoded);
    # Odoo 19 has no native JSON field type for non-translation use.
    raw_payload = fields.Text(
        string="Raw Dutchie Payload",
        help=(
            "JSON-encoded copy of the Dutchie wire item this discount was "
            "synced from. Recovery aid only — do not parse for live use; "
            "use the typed columns above. Updated on every sync."
        ),
    )

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
        # A personal offer granted to one customer that has NO code yet: it
        # shows under "Your personalized rewards" on /rewards and only becomes
        # a live (pending) code — minted + pushed to Dutchie — when the
        # customer slides to redeem it. Nothing exists at the register before
        # that, so a granted offer can never be a dead code.
        ('granted', 'Granted'),
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
        """Return active discounts, optionally filtered by store.

        is_active is the computed Now-flag (published + window + dow + time-of-day).
        is_available_online enforced as a secondary gate.
        """
        domain = [
            ('is_active', '=', True),
            ('is_available_online', '=', True),
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

    # Tolerance for weight equality in grams — Dutchie restrictions are
    # authored as exact values (0.7, 1.0, 3.5), and product weights come
    # from the same source, so 1/100th of a gram is enough headroom.
    _WEIGHT_TOLERANCE_G = 0.01

    @tools.ormcache('root_ids_tuple')
    def _category_descendant_ids_cached(self, root_ids_tuple):
        """Cached resolver: set of product.category ids in the subtree rooted
        at any id in `root_ids_tuple`. Keyed on the tuple so applies_to_product
        across 500 discounts × 1,200 products doesn't re-query Odoo 1.2M times.

        Staleness: Odoo's ormcache auto-invalidates on writes to mint.discount
        (this model), NOT on writes to product.category. Restructuring the
        product category tree mid-process therefore leaves the cache stale
        until the worker recycles. This is accepted because the category tree
        is effectively static post-setup; if that changes, add a write/unlink
        override on product.category that calls
        `self.env['mint.discount']._category_descendant_ids_cached.clear_cache(
            self.env['mint.discount'])`.
        """
        if not root_ids_tuple:
            return frozenset()
        return frozenset(self.env['product.category'].search([
            ('id', 'child_of', list(root_ids_tuple))
        ]).ids)

    def _category_descendant_ids(self, category_ids_m2m):
        """Return the set of product.category ids in the subtree rooted at the
        given m2m. Mirrors Dutchie's behavior where a deal scoped at a master
        (e.g. 'Flower') matches every sub ('Prepack Flower', 'Prerolls', …)."""
        if not category_ids_m2m:
            return frozenset()
        # Sorted tuple so permutations hit the same cache entry.
        key = tuple(sorted(category_ids_m2m.ids))
        return self._category_descendant_ids_cached(key)

    def _product_weight_g(self, product):
        """Product weight in grams. Prefer x_weight_grams (populated by
        Dutchie sync from NetWeight), fall back to parsing the legacy
        x_weight Char ('14.0g', '3.5 g').

        x_weight_grams is a Studio Float that defaults to 0.0 on unsynced
        rows, so we need to distinguish "legitimately zero grams" (never
        happens in the catalog) from "field default because sync hasn't
        run yet". Policy: treat >0 as authoritative, treat 0/None/False as
        "try the legacy Char next." A product with a real zero-gram weight
        has no weight-scoped match anyway.
        """
        val = getattr(product, 'x_weight_grams', None)
        if val is not None and val is not False:
            try:
                fv = float(val)
                if fv > 0:
                    return fv
            except (TypeError, ValueError):
                pass
        raw = getattr(product, 'x_weight', None)
        if raw:
            import re
            m = re.search(r'([0-9]+(?:\.[0-9]+)?)', str(raw))
            if m:
                try:
                    fv = float(m.group(1))
                    if fv > 0:
                        return fv
                except (TypeError, ValueError):
                    pass
        return None

    def _product_vendor_ids(self, product):
        """Return the set of res.partner ids that supply this product
        (via product.supplierinfo / seller_ids)."""
        sellers = getattr(product, 'seller_ids', False)
        if not sellers:
            return set()
        return set(sellers.mapped('partner_id').ids)

    def applies_to_product(self, product):
        """Check if this discount applies to a given product.

        Matching dimensions mirror Dutchie's Reward.Restrictions:
          product, category (with child_of cascade), brand, weight, vendor.
        Each has an inclusion (must match if set) and an exclusion (must NOT
        match if set) pair. SKU blacklist is the one-off flat-text exception.
        """
        self.ensure_one()

        # ── Exclusions ────────────────────────────────────────────────────
        if self.exclude_product_ids and product.id in self.exclude_product_ids.ids:
            return False
        if self.exclude_category_ids:
            excl_tree = self._category_descendant_ids(self.exclude_category_ids)
            if product.categ_id.id in excl_tree:
                return False
        excluded_skus = self._excluded_sku_set()
        if excluded_skus and product.default_code and product.default_code.upper() in excluded_skus:
            return False
        if self.exclude_brand_ids and product.brand_id.id in self.exclude_brand_ids.ids:
            return False
        if self.exclude_vendor_ids:
            vend_ids = self._product_vendor_ids(product)
            if vend_ids & set(self.exclude_vendor_ids.ids):
                return False
        if self.exclude_weight_ids:
            pw = self._product_weight_g(product)
            if pw is not None and any(
                abs(pw - w.value) <= self._WEIGHT_TOLERANCE_G
                for w in self.exclude_weight_ids
            ):
                return False

        # ── Inclusions ────────────────────────────────────────────────────
        if self.product_ids and product.id not in self.product_ids.ids:
            return False
        if self.category_ids:
            incl_tree = self._category_descendant_ids(self.category_ids)
            if product.categ_id.id not in incl_tree:
                return False
        if self.brand_ids and product.brand_id.id not in self.brand_ids.ids:
            return False
        if self.vendor_ids:
            vend_ids = self._product_vendor_ids(product)
            if not (vend_ids & set(self.vendor_ids.ids)):
                return False
        if self.weight_ids:
            pw = self._product_weight_g(product)
            if pw is None:
                return False
            if not any(
                abs(pw - w.value) <= self._WEIGHT_TOLERANCE_G
                for w in self.weight_ids
            ):
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
    def _sibling_redeemable_templates(self, product, stores):
        """The product's sibling templates across ``stores``, itself included.

        Odoo carries one product.template per (product, Dutchie location) — the
        same WYLD gummy at two stores is two templates with two different
        dutchie_product_ids and SKUs. "Sibling" here means: same normalized
        name and same brand, located at one of the given stores. Used to widen
        a redemption's product scope so the coupon matches the product at every
        store its region lock already says it is valid at.

        Matching is deliberately conservative: a false negative just leaves
        that store as unusable as it was before widening existed, while a
        false positive would give the wrong product away free. The SQL
        prefilter is `=ilike` with LIKE metacharacters escaped — case folds,
        but a '%' or '_' in a product name can never wildcard-match unrelated
        products into a 100%-off coupon — and the authoritative comparison is
        the whitespace-normalized equality below.
        """
        if not product:
            return product
        uuids = [u for u in stores.mapped('dutchie_store_id') if u] if stores else []
        escaped = (product.name or '').replace('\\', '\\\\') \
                                      .replace('%', '\\%').replace('_', '\\_')
        domain = [
            ('active', '=', True),
            ('name', '=ilike', escaped),
            ('brand_id', '=', product.brand_id.id if product.brand_id else False),
        ]
        # x_location_id is a DB-created field (no module defines it); without
        # it there is no per-store narrowing, so fall back to just the product.
        if 'x_location_id' not in self.env['product.template']._fields:
            return product
        if uuids:
            domain.append(('x_location_id', 'in', uuids))
        siblings = self.env['product.template'].sudo().search(domain)
        # Whitespace can still differ between two stores' imports ("WYLD  -"
        # vs "WYLD -"); those pass or fail the prefilter as-is, so normalize
        # what it did return rather than trying to widen SQL any further.
        def norm(name):
            return ' '.join((name or '').split()).lower()
        siblings = siblings.filtered(lambda p: norm(p.name) == norm(product.name))
        return (siblings | product) if siblings else product

    @api.model
    def create_redemption(self, partner, points_cost, reward=None, product=None, store=None):
        """Create a loyalty_redemption discount for a customer.

        Caller is responsible for deducting points from the loyalty.card
        inside the same transaction. Supply either `reward` (generic tier)
        or `product` (specific product.template). Returns the new record.

        Per-state/location separation: pass `store` (res.company) — the store
        the customer is redeeming at. The redemption is locked to that store's
        STATE (region) via store_ids, so consume_pending_redemption will only
        honor it at stores in the same market. An empty store_ids means "any
        store" — a cross-state leak — so a store SHOULD always be supplied.
        """
        if not partner:
            raise UserError(_("Partner is required."))
        if not reward and not product:
            raise UserError(_("Either reward or product is required."))

        # Region store set is needed twice: the store_ids lock below, and the
        # sibling-template scope — both must cover the same set of stores.
        state_stores = self.env['res.company'].sudo()
        if store:
            region = store.region_id
            if region:
                state_stores = state_stores.search(
                    [('region_id', '=', region.id), ('is_dispensary', '=', True)])
            state_stores = state_stores or store

        # Widen the coupon's product scope to the redeemed product's SIBLING
        # templates: Dutchie product ids (and SKUs) are per-location, so the
        # same physical product carried by another store in the region lives on
        # a different template with a different id. The coupon is locked
        # region-wide, and /rewards collapses those siblings into one card — so
        # a coupon scoped to just the slid template would be valid at a sibling
        # store yet match nothing in its register. product_ids drives both the
        # Dutchie push restriction (resolved per store there) and
        # _find_matching_free_line; redemption_product_id stays the single
        # template the customer actually slid.
        scope = product
        if product:
            scope = self._sibling_redeemable_templates(product, state_stores)

        code = self._generate_redemption_code()
        label = (product.name if product else None) or (reward.display_name if reward else _("Reward"))
        expires = fields.Datetime.now() + timedelta(days=REDEMPTION_DEFAULT_TTL_DAYS)
        vals = {
            # Code FIRST. This name is what the Dutchie payload builder sends as
            # OnlineName / MenuDisplayName (and as the DiscountDescription
            # fallback), all truncated to 120 chars — so leading with the code
            # keeps it searchable in the Dutchie admin and guarantees it survives
            # truncation on long product names.
            'name': _("%s - Redemption: %s") % (code, label),
            'discount_type': 'loyalty_redemption',
            'is_published': True,
            'monday': True, 'tuesday': True, 'wednesday': True, 'thursday': True,
            'friday': True, 'saturday': True, 'sunday': True,
            'is_available_online': False,
            'valid_from': (fields.Date.context_today(self)
                           - timedelta(days=REDEMPTION_VALID_FROM_SLACK_DAYS)),
            'valid_until': expires.date(),
            # ONE token, two fields. redemption_code is what /rewards shows the
            # customer; dutchie_discount_code is what the pusher sends verbatim
            # as the Dutchie `DiscountCode`. They were previously allowed to
            # diverge — redemption_code was documented as "never sent to
            # Dutchie" while the UI told the customer to read it out at the
            # register — so every code was rejected at the POS. Same value in
            # both, and application_method='code' so Dutchie expects a typed
            # code rather than applying automatically.
            'redemption_code': code,
            'dutchie_discount_code': code,
            'application_method': 'code',
            # SCOPE + VALUE. The Dutchie payload builder reads these off the
            # record (product_ids -> Reward.Restrictions.Product,
            # calculation_method_id/discount_amount -> the Reward). A redemption
            # previously set none of them, so it pushed as a discount with a
            # null value and NO product restriction — Dutchie warned
            # "SOP §10: no scope set", i.e. an unrestricted discount. Express it
            # as what it actually is: 100% off, that product only.
            # DO NOT set calculation_method_id here. mint_dutchie_discount_mirror
            # treats it as the authoritative field and derives discount_type from
            # it, so setting it retyped every redemption to 'percent' — which made
            # the coupon invisible on /rewards and unconsumable at the register,
            # because both queries filter discount_type='loyalty_redemption'.
            # That normalizer is correct; its docstring states redemptions are
            # expected to carry no calc id. The Dutchie reward shape (percent /
            # 100% / single-item) belongs in the payload builder at push time,
            # not on the record.
            'maximum_usage_count': 1,        # one-time
            'max_redemptions': 1,
            'redemption_limit': 1,
            'product_ids': [(6, 0, scope.ids)] if product else [(5, 0, 0)],
            'redemption_partner_id': partner.id,
            'redemption_reward_id': reward.id if reward else False,
            'redemption_product_id': product.id if product else False,
            'redemption_points_cost': points_cost,
            'redemption_status': 'pending',
            'expires_at': expires,
        }
        # Lock to the store's STATE (all dispensary companies in its region), so
        # an AZ-issued reward can't be consumed at a FL/MI store.
        if store:
            vals['store_ids'] = [(6, 0, state_stores.ids or [store.id])]
        else:
            _logger.warning('create_redemption: no store supplied for partner %s — '
                            'redemption is NOT location-locked (redeemable at any store)',
                            partner.id)
        rec = self.sudo().create(vals)
        rec._push_redemption_to_dutchie(store)
        return rec

    @api.model
    def grant_personal_reward(self, partner, product, points_cost,
                              store=None, ttl_days=None):
        """Grant a per-customer offer, visible only to that customer.

        Creates a `granted` loyalty_redemption with NO code: /rewards lists it
        under "Your personalized rewards" for exactly this partner, and the
        customer's slide-to-redeem calls activate_granted_redemption, which is
        when points are charged and the code is minted and pushed to Dutchie.

        Unlike create_redemption, a supplied `store` locks the offer to that
        exact store, not its whole region — grants are targeted offers ("a
        coupon at 75th Ave"), and the activation still region-gates the push.
        Pass a res.company recordset of several stores to widen the lock, or
        no store for redeemable-anywhere.
        """
        if not partner:
            raise UserError(_("Partner is required."))
        if not product and not points_cost:
            raise UserError(_("A product (or at least a points cost) is required."))

        label = product.name if product else _("Personal reward")
        expires = fields.Datetime.now() + timedelta(days=ttl_days or REDEMPTION_DEFAULT_TTL_DAYS)
        vals = {
            'name': _("Personal reward: %s") % label,
            'discount_type': 'loyalty_redemption',
            'is_published': False,
            'monday': True, 'tuesday': True, 'wednesday': True, 'thursday': True,
            'friday': True, 'saturday': True, 'sunday': True,
            'is_available_online': False,
            'valid_from': (fields.Date.context_today(self)
                           - timedelta(days=REDEMPTION_VALID_FROM_SLACK_DAYS)),
            'valid_until': expires.date(),
            'product_ids': [(6, 0, product.ids)] if product else [(5, 0, 0)],
            'redemption_partner_id': partner.id,
            'redemption_product_id': product.id if product else False,
            'redemption_points_cost': points_cost,
            'redemption_status': 'granted',
            'expires_at': expires,
        }
        if store:
            vals['store_ids'] = [(6, 0, store.ids)]
        return self.sudo().create(vals)

    def activate_granted_redemption(self, store=None):
        """Turn a granted personal offer into a live pending redemption code.

        Mirrors what create_redemption does at mint time — same code in both
        code fields, single-use caps, product scope — then pushes to Dutchie.
        The caller (redeem_loyalty) is responsible for the points deduction and
        the push gate, exactly as it is for the create_redemption path.
        """
        self.ensure_one()
        if self.discount_type != 'loyalty_redemption' \
                or self.redemption_status != 'granted':
            raise UserError(_("Not an activatable granted reward."))
        if self.expires_at and self.expires_at < fields.Datetime.now():
            self.sudo().write({'redemption_status': 'expired'})
            raise UserError(_("This personal reward expired on %s.", self.expires_at))

        code = self._generate_redemption_code()
        label = (self.redemption_product_id.name
                 or (self.redemption_reward_id.display_name
                     if self.redemption_reward_id else self.name))
        vals = {
            'name': _("%s - Redemption: %s") % (code, label),
            'redemption_code': code,
            'dutchie_discount_code': code,
            'application_method': 'code',
            'maximum_usage_count': 1,
            'max_redemptions': 1,
            'redemption_limit': 1,
            'redemption_status': 'pending',
            'valid_from': (fields.Date.context_today(self)
                           - timedelta(days=REDEMPTION_VALID_FROM_SLACK_DAYS)),
            # A grant created without a validity end (manual grants skip it)
            # must not activate into one: the Dutchie payload sends
            # valid_until as ValidDateTo, and an empty ValidDateTo makes
            # v2/discount/update-discount-item 500 with a null-reference —
            # the code mints but never reaches the register (observed on
            # prod 2026-08-12, discount 3101). Fall back to the offer's own
            # expiry, then the standard TTL.
            'valid_until': self.valid_until
                or (self.expires_at and self.expires_at.date())
                or fields.Date.today() + timedelta(
                    days=REDEMPTION_DEFAULT_TTL_DAYS),
            'is_published': True,
        }
        # Widen the product scope to sibling templates across the offer's
        # locked stores (or the redeeming store's state when unlocked), same as
        # create_redemption: the grant was stored against ONE template, but the
        # product lives on a different template (different Dutchie id/SKU) at
        # each store the lock allows.
        if self.redemption_product_id:
            lock_stores = self.store_ids
            if not lock_stores and store:
                region = store.region_id
                lock_stores = self.env['res.company'].sudo().search(
                    [('region_id', '=', region.id), ('is_dispensary', '=', True)]
                ) if region else store
            vals['product_ids'] = [(6, 0, self._sibling_redeemable_templates(
                self.redemption_product_id, lock_stores).ids)]
        self.sudo().write(vals)
        self._push_redemption_to_dutchie(store or self.store_ids[:1])
        return self

    def action_push_redemption_to_dutchie(self):
        """Public entry point: (re)create these redemptions in Dutchie.

        Exists because Odoo refuses to dispatch underscore-prefixed methods
        over RPC, and the only public alternative — mint.ptl.day.action_publish
        — republishes that whole day's PTL deals as a side effect. Without this,
        codes already issued to customers could not be repaired without a
        deploy. Safe to re-run: the pusher is keyed on the discount and updates
        dutchie_discount_id in place.
        """
        pushed = 0
        for rec in self:
            if rec.discount_type != 'loyalty_redemption':
                continue
            # The redemption is locked to its state's stores; any one of them
            # carries the region the push gates on.
            if rec._push_redemption_to_dutchie(rec.store_ids[:1]):
                pushed += 1
        return pushed

    def action_deactivate_redemption_in_dutchie(self):
        """Public: pull these redemptions back out of Dutchie (IsDeleted=True).

        Same RPC constraint as the push wrapper — the underlying
        _deactivate_discounts_in_dutchie is private and cannot be dispatched
        remotely, which would leave a bad live discount unfixable without a
        deploy. Targets the push log's (discount, store, dutchie_discount_id)
        rows, so anything never pushed live simply no-ops.
        """
        if 'mint.ptl.day' not in self.env:
            _logger.warning('Deactivate: mint.ptl.day unavailable')
            return 0
        done = 0
        for rec in self:
            region = rec.store_ids[:1].region_id
            day = self.env['mint.ptl.day'].sudo().search(
                [('market_id', '=', region.id)], limit=1) if region else False
            if not day:
                _logger.warning('Deactivate: no ptl.day carrier for %s', rec.redemption_code)
                continue
            try:
                day._deactivate_discounts_in_dutchie(rec)
                done += 1
            except Exception:
                _logger.exception('Deactivate failed for %s', rec.redemption_code)
        return done

    @api.model
    def redemption_push_blocked_reason(self, store=None):
        """Why a redemption at `store` could NOT reach Dutchie, or None if it can.

        Mirrors the gate chain in _push_redemption_to_dutchie and the pusher it
        calls, so it can be asked BEFORE any points are spent.

        This exists because the push deliberately fails soft: by the time it
        runs the points are already deducted and the mint.discount already
        exists in the caller's transaction, so raising there would cost the
        customer their points AND their coupon. The consequence is that a
        blocked push is invisible — the customer is debited and handed a code
        no register will accept. Worse, the market gate returns before any
        push-log row is written, so there is not even an audit trail, and
        nothing sweeps failed pushes for retry.

        Today only Arizona can push: 1 of 43 stores carries
        dutchie_discount_push_enabled and the other five regions have the market
        gate off, while reward catalogs are live in IL/MO/NV. Without this check
        the first redemption in those states silently burns a customer.

        Returns a short reason string for logging, or None when the push would
        go through.
        """
        if 'mint.ptl.day' not in self.env:
            return 'command_center_absent'
        if not store:
            return 'no_store'
        region = store.region_id
        if not region:
            return 'store_has_no_region'
        if not self.env['mint.ptl.day'].sudo().search(
                [('market_id', '=', region.id)], limit=1):
            return 'no_ptl_day_for_market:%s' % (region.code or region.id)
        if not region.dutchie_discount_push_enabled:
            return 'market_push_disabled:%s' % (region.code or region.id)
        if not self.env['res.company'].sudo().search_count([
                ('is_dispensary', '=', True),
                ('dutchie_store_id', '!=', False),
                ('region_id', '=', region.id),
                ('dutchie_discount_push_enabled', '=', True)]):
            return 'no_push_enabled_store_in_market:%s' % (region.code or region.id)
        return None

    def _push_redemption_to_dutchie(self, store=None):
        """Create this redemption as a real discount in Dutchie.

        A code that exists only in Odoo is rejected at the register — the
        budtender types it into Dutchie, which has never heard of it. Pushing
        here is what makes the coupon usable; the welcome pre-roll already does
        the same thing, which is why those codes work and redemptions did not.

        Failures are logged, never raised: the points are already deducted and
        the redemption record already exists inside the caller's transaction, so
        aborting here would cost the customer their points AND their coupon. The
        push log (mint.dutchie.discount.push.log) is the audit trail, and an
        un-pushed redemption still has dutchie_discount_id unset for a sweeper
        to retry.
        """
        self.ensure_one()
        # mint_command_center is not in this module's depends (mint_api_v2 must
        # not require the command centre to install), so probe the registry the
        # same way the identity-key writes elsewhere do.
        if 'mint.ptl.day' not in self.env:
            _logger.warning('Redemption %s: mint.ptl.day unavailable, not pushed '
                            'to Dutchie', self.redemption_code)
            return False
        # _push_discounts_to_dutchie gates on the PTL day's market_id, so it
        # needs a day record carrying the right market. Derive it from the store
        # the customer redeemed at.
        region = store.region_id if store else False
        if not region:
            _logger.warning('Redemption %s: no store/region supplied, not pushed '
                            'to Dutchie (code would be rejected at the register)',
                            self.redemption_code)
            return False
        day = self.env['mint.ptl.day'].sudo().search(
            [('market_id', '=', region.id)], limit=1)
        if not day:
            _logger.warning('Redemption %s: no mint.ptl.day for market %s, not '
                            'pushed to Dutchie', self.redemption_code, region.code)
            return False
        # Fail closed on scope. An unscoped 100%-off discount live at a
        # register discounts ANY item, so refuse to push rather than create it
        # and hope Dutchie is strict. Leaving dutchie_discount_id unset marks it
        # for retry once the product carries a Dutchie id.
        scoped = self.product_ids.filtered(lambda p: p.dutchie_product_id)
        if not scoped:
            _logger.error(
                'Redemption %s: refusing to push — no product scope resolvable '
                '(product_ids=%s). An unscoped 100%%-off discount would apply to '
                'any item.', self.redemption_code, self.product_ids.ids)
            return False
        try:
            day._push_discounts_to_dutchie(self.ids)
        except Exception:
            _logger.exception('Redemption %s: Dutchie push failed; the code will '
                              'not work at the register until re-pushed',
                              self.redemption_code)
            return False
        # A successful upsert proves Dutchie ACCEPTED the payload, not that the
        # code works at a register — a hollow push returns ok:true and then
        # fails at the till. Read it back before trusting it.
        return self._verify_redemption_in_dutchie()

    def _verify_redemption_in_dutchie(self):
        """Read the discount back out of Dutchie and assert it is usable.

        Ground truth check: fetches the stored record via
        GET /api/admin/discounts/<id> and asserts it has a code, a value, a
        product scope, and no online-platform lock. Returns True only when
        Dutchie itself confirms all four.

        The per-(discount, store) Dutchie id lives on the push log row, not on
        mint.discount — a single field cannot represent N per-store ids, and
        caching it there previously made store N target store 1's discount.
        """
        self.ensure_one()
        Log = self.env['mint.dutchie.discount.push.log'].sudo()
        row = Log.search([('discount_id', '=', self.id),
                          ('dutchie_discount_id', '!=', 0),
                          ('success', '=', True)], order='id desc', limit=1)
        if not row:
            _logger.error('Redemption %s: no successful push log row with a '
                          'Dutchie id — treating as NOT verified',
                          self.redemption_code)
            return False
        store = row.company_id
        loc_id = getattr(store, 'dutchie_pos_location_id', 0)
        lsp_id = store._dutchie_lsp()
        if not (loc_id and lsp_id):
            _logger.error('Redemption %s: store %s missing loc/lsp id, cannot verify',
                          self.redemption_code, store.display_name)
            return False

        ICP = self.env['ir.config_parameter'].sudo()
        base = (ICP.get_param('mint.dutchie_discount_push.url')
                or 'https://mintinvsvc-production-6aa5.up.railway.app/api/admin/discounts')
        api_key = ICP.get_param('mint.dutchie_discount_push.api_key') or ''
        # The scope may hold one sibling template per store (per-location
        # Dutchie ids), and the payload builder prefers the template located at
        # the store it pushes to — so expect the id the same preference picks
        # for THIS row's store, or any scoped id when none is store-local.
        scoped = self.product_ids.filtered(lambda p: p.dutchie_product_id)
        loc_uuid = getattr(store, 'dutchie_store_id', False)
        local = scoped.filtered(lambda p: p.x_location_id == loc_uuid) \
            if loc_uuid and 'x_location_id' in scoped._fields else scoped.browse()
        product = (local or scoped)[:1]
        url = (
            '%s/%s?locId=%s&lspId=%s&expectCode=%s' % (
                base.rstrip('/'), row.dutchie_discount_id, loc_id, lsp_id,
                url_quote(self.redemption_code or ''))
        )
        if product and product.dutchie_product_id:
            url += '&expectProductId=%s' % url_quote(str(product.dutchie_product_id))

        req = urlrequest.Request(url, headers={'x-api-key': api_key})
        try:
            with urlrequest.urlopen(req, timeout=45) as resp:
                payload = json.loads(resp.read().decode('utf-8', errors='replace'))
        except HTTPError as e:
            # 422 is the endpoint's "found it, but it is not usable" answer and
            # carries the failed checks — worth logging in full.
            detail = ''
            try:
                detail = e.read().decode('utf-8', errors='replace')[:400]
            except Exception:
                pass
            _logger.error('Redemption %s: Dutchie verify FAILED (%s) %s',
                          self.redemption_code, e.code, detail)
            return False
        except Exception:
            _logger.exception('Redemption %s: Dutchie verify call errored',
                              self.redemption_code)
            return False

        if payload.get('verified'):
            _logger.info('Redemption %s verified in Dutchie (discount %s)',
                         self.redemption_code, row.dutchie_discount_id)
            return True
        _logger.error('Redemption %s: Dutchie says NOT usable — failed checks %s',
                      self.redemption_code, payload.get('failed_checks'))
        return False

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
                'is_published': False,
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
            # Refund the deducted points regardless of whether this was a
            # reward-based or a product-based redemption. The product redeem
            # path (create_redemption(product=...)) never sets
            # redemption_reward_id, so the old `if reward` gate silently kept
            # the customer's points on void. Resolve the program from the
            # reward when present, else the same way redeem_loyalty found the
            # card to deduct from: the loyalty-type program.
            reward = rec.redemption_reward_id
            if rec.redemption_partner_id and rec.redemption_points_cost:
                program = reward.program_id if reward else self.env['loyalty.program'].sudo().search(
                    [('program_type', '=', 'loyalty')], limit=1)
                if program:
                    card = LoyaltyCard.search([
                        ('partner_id', '=', rec.redemption_partner_id.id),
                        ('program_id', '=', program.id),
                    ], limit=1)
                    if card:
                        card.points = card.points + rec.redemption_points_cost
            rec.write({
                'redemption_status': 'voided',
                'is_published': False,
            })
        return True

    @api.model
    def consume_pending_redemption(self, partner, order_items=None, store=None):
        """Consume the pending redemption that this order actually honored.

        Strict path (preferred): caller passes ``order_items`` — a list of
        dicts with at least ``dutchie_product_id`` or ``sku``, plus
        ``unit_price``, ``discount``, ``quantity``. A redemption is
        consumed only when the order contains a line matching its
        redemption_product_id AND that line is effectively free
        (unit_price*qty - discount <= $0.01). Prevents blindly marking a
        redemption used when the budtender didn't actually honor it.

        Legacy path: when ``order_items`` is None, falls back to
        oldest-pending with a warning log.
        """
        if not partner:
            return self.browse()

        domain = [
            ('discount_type', '=', 'loyalty_redemption'),
            ('redemption_partner_id', '=', partner.id),
            ('redemption_status', '=', 'pending'),
        ]
        # Per-state/location enforcement: only honor a redemption whose locked
        # store set (its state) includes THIS order's store. store_ids empty =
        # legacy/unlocked redemption, honored anywhere (back-compat). Without a
        # store we cannot enforce location — log and fall through (legacy).
        if store:
            domain += ['|', ('store_ids', '=', False), ('store_ids', 'in', store.id)]
        else:
            _logger.warning('consume_pending_redemption: no store supplied for '
                            'partner %s — location lock NOT enforced', partner.id)
        pending = self.sudo().search(domain, order='create_date asc')
        if not pending:
            return self.browse()

        now = fields.Datetime.now()
        expired = pending.filtered(lambda r: r.expires_at and r.expires_at < now)
        if expired:
            expired.write({'redemption_status': 'expired', 'is_published': False})
        fresh = pending - expired
        if not fresh:
            return self.browse()

        if order_items is None:
            _logger.warning(
                'consume_pending_redemption called without order_items for '
                'partner id=%s; using oldest-pending heuristic',
                partner.id,
            )
            redemption = fresh[:1]
            redemption.write({
                'redemption_status': 'used',
                'redemption_used_at': now,
                'is_published': False,
            })
            return redemption

        for redemption in fresh:
            matched_line = _find_matching_free_line(redemption, order_items)
            if matched_line is not None:
                redemption.write({
                    'redemption_status': 'used',
                    'redemption_used_at': now,
                    'is_published': False,
                })
                _logger.info(
                    'Consumed redemption %s (product=%s) for partner %s '
                    'against matching order line',
                    redemption.redemption_code,
                    redemption.redemption_product_id.display_name,
                    partner.id,
                )
                return redemption

        _logger.info(
            'No free order line matched any of %d pending redemption(s) '
            'for partner %s; leaving all pending',
            len(fresh), partner.id,
        )
        return self.browse()

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
            stale.write({'redemption_status': 'expired', 'is_published': False})
        return len(stale)
