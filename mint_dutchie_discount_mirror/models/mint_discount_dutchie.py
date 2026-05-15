from odoo import fields, models


class MintDiscountDutchie(models.Model):
    """Mirror the Dutchie Backoffice discount structure onto mint.discount.

    All new fields are optional and only populated when source='dutchie'.
    The sync worker keys upserts on `dutchie_discount_id` (already defined
    on the base model as Char).
    """
    _inherit = 'mint.discount'

    # ---- Daily time window (hours-of-day as floats, e.g. 13.0 = 1pm) ----
    start_time = fields.Float(string="Start Time", help="Daily start hour (0-24)")
    end_time = fields.Float(string="End Time", help="Daily end hour (0-24)")

    # ---- Reward calculation detail ----
    # Extend the base discount_type selection with Dutchie-native values.
    discount_type = fields.Selection(
        selection_add=[
            ('price_per_unit', 'Price per Unit'),
            ('dollar_off_total', 'Dollar Off Total'),
            ('bundle', 'Bundle'),
        ],
        ondelete={
            'price_per_unit': 'set default',
            'dollar_off_total': 'set default',
            'bundle': 'set default',
        },
    )
    calculation_method_id = fields.Integer(
        string="Dutchie Calc Method ID",
        help="Raw Dutchie CalculationMethodId (1=percent, 2=$ off, 3=price, "
             "5=$ off total, 6=price per unit, 15=bundle)",
    )
    discount_value = fields.Float(
        string="Raw Reward Value",
        help="Raw Dutchie Reward.DiscountValue (interpretation depends on CalcMethod)",
    )
    highest_or_lowest = fields.Selection(
        [('low', 'Apply to Lowest'), ('high', 'Apply to Highest')],
        string="Apply Reward To",
    )
    apply_to_only_one_item = fields.Boolean(string="Apply to Only One Item")
    item_group_type_id = fields.Integer(string="Dutchie Item Group Type ID")

    # ---- Application / stacking / approval ----
    application_method = fields.Selection(
        [('automatic', 'Automatic'), ('manual', 'Manual'), ('code', 'Code')],
        string="Application Method",
    )
    can_stack_automatically = fields.Boolean(string="Can Stack Automatically")
    require_manager_approval = fields.Boolean(string="Requires Manager Approval")
    dutchie_is_deleted = fields.Boolean(string="Deleted in Dutchie")
    include_non_cannabis = fields.Boolean(
        string="Includes Non-Cannabis",
        default=False,
        help="When True, this discount applies to non-cannabis products "
             "(accessories, merch) in addition to cannabis SKUs. Dutchie "
             "populates this on 100% of records — captured for parity so "
             "'20% off everything' deals preserve their scope.",
    )

    # ---- Redemption limits ----
    max_redemptions = fields.Integer(string="Max Redemptions")
    redemption_limit = fields.Integer(string="Redemption Limit")
    redemption_limit_period = fields.Char(string="Redemption Limit Period")
    redemption_limit_counting_mode = fields.Integer(string="Redemption Counting Mode")

    # ---- Raw Dutchie restriction IDs ----
    # The FK many2many fields (brand_ids, category_ids, product_ids,
    # exclude_*_ids) need Dutchie-id → Odoo-id resolution, which may be
    # incomplete. These raw fields preserve the source-of-truth Dutchie IDs
    # so data is never lost and resolution can be retried later.
    brand_restriction_ids_raw = fields.Char(
        string="Dutchie Brand Restrictions (raw)",
        help="Pipe-joined Dutchie brand IDs (prefix 'include:' or 'exclude:')",
    )
    category_restriction_ids_raw = fields.Char(
        string="Dutchie Category Restrictions (raw)",
    )
    product_restriction_ids_raw = fields.Char(
        string="Dutchie Product Restrictions (raw)",
    )
    inventory_tag_ids_raw = fields.Char(string="Inventory Tag IDs (raw)")
    weight_restrictions_raw = fields.Char(string="Weight Restrictions (raw)")
    customer_type_restriction = fields.Selection(
        [('rec', 'Recreational'), ('med', 'Medical'), ('both', 'Both')],
        string="Customer Type",
    )
    order_type_restrictions_raw = fields.Char(string="Order Types (raw)")
    platform_type_restrictions_raw = fields.Char(string="Platforms (raw)")
    payment_restrictions_raw = fields.Char(string="Payments (raw)")

    # ---- Display / external metadata ----
    menu_display_name = fields.Char(string="Menu Display Name")
    online_name = fields.Char(string="Online Name")
    menu_display_rank = fields.Integer(string="Menu Display Rank")
    menu_display_image_url = fields.Char(string="Menu Display Image URL")
    external_id = fields.Char(string="External ID")
    loyalty_point_value = fields.Float(string="Loyalty Point Value")

    # ---- Brand-funded promo tracking ----
    brand_funded_dutchie_brand_id = fields.Integer(
        string="Brand-Funded Dutchie Brand ID",
        help="Raw Dutchie BrandId for brand-funded promos (no FK; mint.brand "
             "link lives on brand_ids).",
    )
    brand_funded_amount = fields.Float(string="Brand Funded Amount")
    brand_funded_percent = fields.Float(string="Brand Funded Percent")
