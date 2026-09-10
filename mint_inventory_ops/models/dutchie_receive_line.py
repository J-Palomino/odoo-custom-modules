"""One product line on a Dutchie inventory receive.

`sku` is the load-bearing field. Dutchie matches an incoming line to a real
ProductId by SKU and nothing else, so a line without one is dead weight — the
parent refuses to validate if any line is missing it. `product_id` is optional
and only there to autofill from the Odoo catalogue when the product happens to
exist on both sides; a delivery can be typed in from the invoice with no Odoo
product at all.
"""
from odoo import _, api, fields, models

# Mirrors product.template.master_category EXACTLY — same keys, same labels.
# Kept as a literal rather than read off the other field so a receive line and
# a product agree even if one module is upgraded before the other; if that
# field gains a value, add it here too.
MASTER_CATEGORIES = [
    ('flower', 'Flower'),
    ('vaporizers', 'Vaporizers'),
    ('concentrates', 'Concentrates'),
    ('edibles', 'Edibles'),
    ('tinctures', 'Tinctures'),
    ('topicals', 'Topicals'),
    ('accessories', 'Accessories'),
    ('prerolls', 'Pre-Rolls'),
    ('beverages', 'Beverages'),
]

# Units for the WCIA manifest's `uom` field. These are MANIFEST-side and are
# NOT Dutchie's UnitId enum — Dutchie resolves the real unit from the matched
# product and returns it as UnitId on the preview. No canonical UnitId list is
# published, so this list deliberately does not pretend to be one.
UOM_UNITS = [
    ('ea', 'each'),
    ('g', 'grams'),
    ('mg', 'milligrams'),
    ('oz', 'ounces'),
    ('lb', 'pounds'),
    ('ml', 'millilitres'),
]

# --- Dutchie taxonomy -> master_category -------------------------------------
#
# Master Category is filled from Dutchie, preferring its ECOMMERCE taxonomy and
# falling back to its POS one.
#
# EcomCategory first because it is near-1:1 with the list above and is stable
# across markets. MasterCategory (the POS taxonomy) is ~100% populated but
# encodes the MARKET as much as the product — MI files pre-rolls under a
# dedicated `Prerolls` master and infused flower under `Inhalable Compound
# Concentrate`, while AZ and FL put both under `Flower`. Mapping from it alone
# would make the same product land in different categories per state.
#
# Coverage measured across all six LSPs: EcomCategory 83.1%, POS fallback the
# remaining 16.9%; together they cover every consumer-facing product. What is
# left unmapped is samples, bulk and packaging, which should not carry a
# consumer category at all.
#
# Vocabulary verified live 2026-08-27 against LSP 575, not taken from docs.
ECOM_CATEGORY_TO_MASTER = {
    'flower': 'flower',
    'vaporizers': 'vaporizers',
    'concentrate': 'concentrates',
    'edible': 'edibles',
    'tincture': 'tinctures',
    'topicals': 'topicals',
    'accessories': 'accessories',
    'apparel': 'accessories',
    'pre-rolls': 'prerolls',
}

# 🚨 NOT categories — Dutchie's menu-visibility flags, which appear in the same
# field. Seen live: 223 'Hide' and 85 'N/A' in AZ alone, and SKU 30958096 (OOZE
# Hot Knife) comes back 'Hide'. Treated as "no answer" so the POS fallback runs.
ECOM_CONTROL_FLAGS = {'hide', 'n/a', 'na'}

# Odoo has `beverages`; Dutchie has no EcomCategory for it. Drinks live one
# level down, as an EcomSubcategory under Edible (76 products in AZ).
ECOM_SUBCATEGORY_TO_MASTER = {
    ('edible', 'drinks'): 'beverages',
}

# POS MasterCategory -> master_category. Only the unambiguous ones.
# Deliberately absent: Unmedicated, Medicated, Non Medicated, Infused
# Non-Edible and PROMO. Those are merchandising flags with no honest equivalent
# in a consumer taxonomy — guessing one would file swag or promos as product.
POS_MASTER_CATEGORY_TO_MASTER = {
    'flower': 'flower',
    'vape': 'vaporizers',
    'concentrate': 'concentrates',
    # FL, IL and NV emit the PLURAL. Without these, rows as unambiguous as
    # "Concentrates / CNC-Distillate Syringe" and "Edibles / Beverages" fell
    # through to False — measured 2026-08-28 against 6,618 live rows.
    'concentrates': 'concentrates',
    'edibles': 'edibles',
    # Same plural class, missed on the first pass and caught in PRODUCTION by
    # prod-taxonomy-contract N1: `tinctures` went 111 -> 99 the moment the sync
    # shipped. Twelve real tinctures (Chronic Health Tincture, Drip Drops,
    # Canine CBD Relief Tincture) carry POS master `Medicated`/`Unmedicated` —
    # merchandising flags tier 2 correctly refuses — and no EcomCategory, so
    # they fall to tier 3, where only the SINGULAR 'tincture' existed. The
    # "- Unmedicated" forms are the sheets' own Unmedicated sub-categories for
    # CBD tinctures and topicals; the storefront's categoryMapping already
    # files all four under Tinctures/Topicals, so this agrees with it rather
    # than inventing a second opinion.
    'tinctures': 'tinctures',
    'topical': 'topicals',
    'tinctures - unmedicated': 'tinctures',
    'topicals - unmedicated': 'topicals',
    'inhalable compound concentrate': 'concentrates',
    'edible': 'edibles',
    'infused edible': 'edibles',
    'prerolls': 'prerolls',
    'pre-rolls': 'prerolls',
    'accessories': 'accessories',
    'tincture': 'tinctures',
    'topicals': 'topicals',
    # Tier-3 in practice: these never appear as a POS MasterCategory, but they
    # are common fine-grained Categories sitting under the merchandising
    # masters we refuse to map. 40 live rows of "Unmedicated / Clothing" and 18
    # of "Non Medicated / CBD" resolved to nothing. Both are already
    # Accessories in the storefront's categoryMapping, so this agrees with it
    # rather than inventing a new opinion.
    'clothing': 'accessories',
    'cbd': 'accessories',
}


def master_category_from_dutchie(ecom_category, ecom_subcategory,
                                 pos_master_category, pos_category=None):
    """Resolve Dutchie's categories to an Odoo master_category key.

    Three tiers, most trustworthy first:

      1. EcomCategory — stable across markets, near-1:1 with our list
      2. POS MasterCategory — ~100% populated but market-flavoured
      3. POS Category (the fine-grained one), EXACT matches only

    Tier 3 exists because tiers 1 and 2 both miss a real case: SKU 30958096
    (OOZE Hot Knife) comes back EcomCategory 'Hide' — a visibility flag — and
    MasterCategory 'Unmedicated', a merchandising flag with no product meaning,
    while its actual Category is the unambiguous 'Accessories'. Exact-match
    only: Dutchie's fine-grained vocabulary is things like 'Cured Resin' and
    'Cartridge: Distillate', which correctly match nothing here.

    An unmappable value yields False so the field stays EMPTY rather than
    wrong. A plausible-but-guessed category is worse than none — someone has to
    notice it is wrong before they can correct it.
    """
    ecom = (ecom_category or '').strip().lower()
    sub = (ecom_subcategory or '').strip().lower()

    if ecom and ecom not in ECOM_CONTROL_FLAGS:
        special = ECOM_SUBCATEGORY_TO_MASTER.get((ecom, sub))
        if special:
            return special
        mapped = ECOM_CATEGORY_TO_MASTER.get(ecom)
        if mapped:
            return mapped

    pos = (pos_master_category or '').strip().lower()
    mapped = POS_MASTER_CATEGORY_TO_MASTER.get(pos)
    if mapped:
        return mapped

    fine = (pos_category or '').strip().lower()
    return POS_MASTER_CATEGORY_TO_MASTER.get(fine, False)


class DutchieReceiveLine(models.Model):
    _name = 'mint.dutchie.receive.line'
    _description = 'Dutchie Inventory Receive Line'
    _order = 'sequence, id'

    receive_id = fields.Many2one(
        'mint.dutchie.receive', string='Receive',
        required=True, ondelete='cascade', index=True,
    )
    sequence = fields.Integer(default=10)

    product_id = fields.Many2one(
        'product.product', string='Odoo Product',
        help='Optional. Only used to autofill SKU, name and cost — the receive '
             'is driven by the SKU, not by this link.',
    )
    sku = fields.Char(
        string='SKU', required=True,
        help='Dutchie matches on this and only this. Copy it from the invoice '
             'or the vendor manifest exactly.',
    )
    product_name = fields.Char(string='Product Name')

    quantity = fields.Float(string='Quantity', default=1.0, required=True)
    unit_cost = fields.Float(string='Unit Cost', digits='Product Price')
    unit_id = fields.Integer(
        string='Dutchie UnitId', default=1, readonly=True,
        help='Dutchie unit-of-measure id, resolved from the matched product '
             'during Validate — Dutchie owns this, which is why it is not '
             'editable. 1 = each, correct for almost everything.',
    )
    uom = fields.Selection(
        UOM_UNITS, string='UoM', default='ea',
        help='Unit written into the WCIA manifest. Not the same thing as '
             'Dutchie UnitId, which Dutchie fills in from the product it '
             'matched.',
    )
    net_weight = fields.Float(
        string='Net Weight', help='Autofilled from the Odoo product.',
    )

    # Cannabis / compliance detail carried into the WCIA manifest
    batch_number = fields.Char(string='Batch / Lot')
    package_id = fields.Char(
        string='Package ID',
        help='Vendor/Metrc package identifier for this line.',
    )
    strain = fields.Char(
        string='Strain',
        help='Autofilled from the Odoo product. Left free-text because a vendor '
             'can deliver a strain the catalogue has not seen yet.',
    )
    master_category = fields.Selection(
        MASTER_CATEGORIES, string='Master Category',
        help="Top level of Dutchie's taxonomy. Distinct from Category (e.g. "
             "Flower vs Prepack Flower) — the manifest carries both, and "
             "collapsing them loses the top level.",
    )
    category = fields.Char(
        string='Category',
        help='Finer-grained category. Autofilled from the Odoo product rather '
             'than picked from a list: the catalogue holds 500+ free-text '
             'variants ("Accessories"/"ACCESSORIES", "Concentrate"/'
             '"Concentrates"), so a dropdown over them would offer the mess '
             'rather than fix it. Clean that up on the product, not here.',
    )
    thc = fields.Char(string='THC', help='Autofilled from the Odoo product.')
    cbd = fields.Char(string='CBD', help='Autofilled from the Odoo product.')
    lab_result_link = fields.Char(
        string='COA Link',
        help='Certificate of analysis URL. Carried into the manifest lab result '
             'block, which Dutchie requires to be present on every line.',
    )
    room_ref_id = fields.Many2one(
        'mint.dutchie.room', string='Room Override', ondelete='restrict',
        domain="[('loc_id', '=', parent.pos_location_id)]",
        help="Only for a line that lands somewhere other than the receive's "
             'destination room. Leave empty for the normal case. The list is '
             "the store's real Dutchie rooms — load them with Load Rooms & "
             'Vendors on the receive.',
    )

    # Filled by Validate
    dutchie_product_id = fields.Integer(
        string='Dutchie ProductId', readonly=True, copy=False,
        help='Resolved from the SKU by Dutchie during Validate. Zero means the '
             'SKU did not match anything at this location.',
    )
    dutchie_product_name = fields.Char(
        string='Dutchie Product', readonly=True, copy=False,
    )
    resolve_note = fields.Char(
        string='Match Note', readonly=True, copy=False,
        help='Why this line did not resolve, when it did not.',
    )
    resolved = fields.Boolean(
        string='Matched', compute='_compute_resolved', store=True,
    )

    @api.depends('dutchie_product_id')
    def _compute_resolved(self):
        for line in self:
            line.resolved = bool(line.dutchie_product_id)

    @api.onchange('product_id')
    def _onchange_product_id(self):
        """Hard-match the descriptive fields to the Odoo product.

        This is what keeps Category / Strain / THC / CBD / Net Weight from
        being retyped (and mistyped) on every delivery — they are catalogue
        facts about the product, not facts about this shipment. Only the
        genuinely per-delivery fields (SKU is per-product, but Batch, Package
        ID, Quantity and Cost change every time) are left to the user.

        Existing values are never clobbered: a line already carrying a vendor's
        own figure keeps it. Clear the field to re-pull from the product.
        """
        for line in self:
            product = line.product_id
            if not product:
                continue
            if not line.sku:
                line.sku = product.default_code or ''
            if not line.product_name:
                line.product_name = product.name or ''
            if not line.unit_cost:
                line.unit_cost = product.standard_price or 0.0
            # master_category is deliberately NOT filled from the Odoo product.
            # It now comes from Dutchie on Validate (EcomCategory, falling back
            # to the POS master category) — see master_category_from_dutchie.
            # Dutchie is the system this receive is being written into, so its
            # answer is the one that should decide, and seeding a guess here
            # would only get overwritten or, worse, silently kept when Dutchie
            # had no answer.
            if not line.category:
                line.category = product.x_category or ''
            if not line.strain:
                line.strain = product.strain or product.x_strain or ''
            if not line.thc:
                line.thc = product.thc or product.x_thc or ''
            if not line.cbd:
                line.cbd = product.cbd or product.x_cbd or ''
            if not line.net_weight:
                # weight is the Odoo-native field; x_weight_grams is the
                # Dutchie-synced one and is the better answer when both exist.
                line.net_weight = product.x_weight_grams or product.weight or 0.0

    @api.onchange('sku', 'quantity', 'unit_cost')
    def _onchange_invalidate_match(self):
        """A line edited after validation is no longer validated.

        Silently keeping a stale ProductId next to a changed SKU is how a wrong
        product gets received, so the match is dropped the moment the key
        changes and the parent has to be re-validated.
        """
        for line in self:
            if line.dutchie_product_id:
                line.dutchie_product_id = 0
                line.dutchie_product_name = ''
                line.resolve_note = _('Line edited — re-validate')

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def _master_category_label(self):
        """Selection key -> the label Dutchie's taxonomy uses ('flower' -> 'Flower')."""
        self.ensure_one()
        if not self.master_category:
            return ''
        return dict(MASTER_CATEGORIES).get(self.master_category, '')

    def _to_manifest_dict(self):
        """Shape consumed by the WCIA manifest builder in mintinvsvc.

        Deliberately flat and stringly-typed: the builder is responsible for the
        WCIA snake_case layout and, critically, for emitting `lab_result_data`
        on every item and never emitting a JSON null (both are what made
        Dutchie's parser null-dereference for ~14 rounds of probing).
        """
        self.ensure_one()
        return {
            'sku': (self.sku or '').strip(),
            'product_name': self.product_name or self.product_id.name or '',
            'quantity': self.quantity,
            'unit_cost': self.unit_cost,
            'uom': self.uom or 'ea',
            'net_weight': self.net_weight or 0.0,
            'batch_number': self.batch_number or '',
            'package_id': self.package_id or '',
            'strain': self.strain or '',
            # The manifest carries the human label ("Flower"), not the Odoo
            # selection key ("flower") — Dutchie matches on the display form.
            'master_category': self._master_category_label(),
            'category': self.category or '',
            'thc': self.thc or '',
            'cbd': self.cbd or '',
            'lab_result_link': self.lab_result_link or '',
        }

    def _to_receive_product(self):
        """Shape consumed by v2/inventory/receive (PascalCase, as Dutchie wants).

        RoomId is left off when there is no override so invsvc applies the
        receive-level defaultRoomId.
        """
        self.ensure_one()
        product = {
            'ProductId': self.dutchie_product_id,
            'Sku': (self.sku or '').strip(),
            'ProductName': self.dutchie_product_name or self.product_name or '',
            'Quantity': self.quantity,
            'UnitCost': self.unit_cost,
            'UnitId': self.unit_id or 1,
            'Category': self.category or '',
            'NetWeight': self.net_weight or 0.0,
        }
        if self.room_ref_id:
            product['RoomId'] = self.room_ref_id.dutchie_room_id
        return product
