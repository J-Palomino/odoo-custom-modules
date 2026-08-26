"""One product line on a Dutchie inventory receive.

`sku` is the load-bearing field. Dutchie matches an incoming line to a real
ProductId by SKU and nothing else, so a line without one is dead weight — the
parent refuses to validate if any line is missing it. `product_id` is optional
and only there to autofill from the Odoo catalogue when the product happens to
exist on both sides; a delivery can be typed in from the invoice with no Odoo
product at all.
"""
from odoo import _, api, fields, models


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
        string='Dutchie UnitId', default=1,
        help='Dutchie unit-of-measure id. 1 = each, which is right for almost '
             'everything; resolved from Dutchie on Validate.',
    )
    uom = fields.Char(string='UoM', default='ea')
    net_weight = fields.Float(string='Net Weight')

    # Cannabis / compliance detail carried into the WCIA manifest
    batch_number = fields.Char(string='Batch / Lot')
    package_id = fields.Char(
        string='Package ID',
        help='Vendor/Metrc package identifier for this line.',
    )
    strain = fields.Char(string='Strain')
    master_category = fields.Char(
        string='Master Category',
        help="Top level of Dutchie's taxonomy (e.g. Flower). Distinct from "
             "Category (e.g. Prepack Flower) — the manifest carries both, and "
             "collapsing them loses the top level.",
    )
    category = fields.Char(string='Category')
    thc = fields.Char(string='THC')
    cbd = fields.Char(string='CBD')
    lab_result_link = fields.Char(
        string='COA Link',
        help='Certificate of analysis URL. Carried into the manifest lab result '
             'block, which Dutchie requires to be present on every line.',
    )
    room_id_override = fields.Integer(
        string='RoomId Override',
        help='Leave 0 to use the receive-level destination room.',
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
        """Autofill from the Odoo catalogue without clobbering typed-in values."""
        for line in self:
            if not line.product_id:
                continue
            if not line.sku:
                line.sku = line.product_id.default_code or ''
            if not line.product_name:
                line.product_name = line.product_id.name or ''
            if not line.unit_cost:
                line.unit_cost = line.product_id.standard_price or 0.0

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
            'master_category': self.master_category or '',
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
        if self.room_id_override:
            product['RoomId'] = self.room_id_override
        return product
