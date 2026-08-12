# -*- coding: utf-8 -*-
"""Add Item wizard — push a product onto a live Dutchie POS cart from Odoo.

Budtender/manager opens a swimlane order, searches the store's
preorder-eligible catalog, and adds an item. Both calls go through the
inventory service (mintinvsvc), which owns the Dutchie Backoffice session
and resolves the store's LSP tenant from its LocId:

    POST <invsvc>/api/pos/search-products  { locId, query, limit }
    POST <invsvc>/api/pos/add-item         { locId, shipmentId, productId,
                                             quantity, productName, unitPrice }

Config (ir.config_parameter):
    mint_pos_dutchie.invsvc_api_url   base URL, no default — unset means the
                                      feature is off (mirrors the lane-change
                                      webhook's no-default rule so staging
                                      can't write into prod carts).
    mint_pos_dutchie.invsvc_api_key   falls back to
                                      mint_pos_dutchie.lane_change_webhook_api_key
                                      (same mintinvsvc API_KEY secret).
"""

import json
import logging
import urllib.error
import urllib.request

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_S = 25


class MintPosAddItemWizard(models.TransientModel):
    _name = 'mint.pos.add.item.wizard'
    _description = 'Add Item to Live POS Cart'

    order_id = fields.Many2one(
        'mint.pos.order', string='Order', required=True, readonly=True,
        ondelete='cascade',
    )

    @api.model
    def default_get(self, fields_list):
        # Support the "Action" menu binding on mint.pos.order, which passes
        # active_id instead of default_order_id.
        res = super().default_get(fields_list)
        if 'order_id' in fields_list and not res.get('order_id'):
            if self.env.context.get('active_model') == 'mint.pos.order':
                res['order_id'] = self.env.context.get('active_id')
        return res
    query = fields.Char(
        string='Search',
        help='Product name, SKU, barcode, or METRC tag',
    )
    quantity = fields.Float(string='Quantity', default=1.0)
    result_ids = fields.One2many(
        'mint.pos.add.item.wizard.line', 'wizard_id', string='Results',
    )

    # ------------------------------------------------------------------
    # invsvc plumbing
    # ------------------------------------------------------------------

    def _invsvc_config(self):
        ICP = self.env['ir.config_parameter'].sudo()
        base_url = (ICP.get_param('mint_pos_dutchie.invsvc_api_url', '') or '').rstrip('/')
        api_key = (
            ICP.get_param('mint_pos_dutchie.invsvc_api_key', '')
            or ICP.get_param('mint_pos_dutchie.lane_change_webhook_api_key', '')
        )
        if not base_url:
            raise UserError(
                'POS cart integration is not configured '
                '(mint_pos_dutchie.invsvc_api_url is unset).'
            )
        return base_url, api_key

    def _invsvc_post(self, path, payload):
        base_url, api_key = self._invsvc_config()
        req = urllib.request.Request(
            base_url + path,
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json', 'X-Api-Key': api_key},
            method='POST',
        )
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as err:
            try:
                detail = json.loads(err.read().decode('utf-8')).get('error', '')
            except Exception:
                detail = ''
            raise UserError(
                'Inventory service rejected the request (HTTP %s). %s'
                % (err.code, detail)
            ) from err
        except Exception as err:
            raise UserError('Inventory service unreachable: %s' % err) from err

    def _get_loc_id(self):
        self.ensure_one()
        config = self.env['mint.web.order.config'].sudo().search(
            [('company_id', '=', self.order_id.company_id.id), ('active', '=', True)],
            limit=1,
        )
        if not config or not config.dutchie_loc_id:
            raise UserError(
                'No active web order config (Dutchie LocId) for %s.'
                % self.order_id.company_id.name
            )
        return config.dutchie_loc_id

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _check_order_open(self):
        self.ensure_one()
        if not self.order_id.dutchie_shipment_id:
            raise UserError('This order has no Dutchie shipment — nothing to add to.')
        if self.order_id.state in ('completed', 'cancelled'):
            raise UserError('Order is %s — the POS cart is closed.' % self.order_id.state)

    def _reopen(self):
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_search(self):
        self.ensure_one()
        self._check_order_open()
        if not (self.query or '').strip():
            raise UserError('Enter a product name, SKU, barcode, or METRC tag.')
        loc_id = self._get_loc_id()
        result = self._invsvc_post('/api/pos/search-products', {
            'locId': loc_id,
            'query': self.query.strip(),
            'limit': 20,
        })
        self.result_ids.unlink()
        lines = []
        for p in result.get('products', []):
            lines.append({
                'wizard_id': self.id,
                'dutchie_product_id': str(p.get('ProductId') or ''),
                'name': p.get('ProductName') or p.get('Name') or '',
                'sku': p.get('Sku') or p.get('ProductNo') or '',
                'brand': p.get('BrandName') or '',
                'category': p.get('Category') or '',
                'price': p.get('RecUnitPrice') or p.get('UnitPrice') or p.get('Price') or 0.0,
            })
        if lines:
            self.env['mint.pos.add.item.wizard.line'].create(lines)
        return self._reopen()


class MintPosAddItemWizardLine(models.TransientModel):
    _name = 'mint.pos.add.item.wizard.line'
    _description = 'Add Item Wizard Search Result'
    _order = 'id'

    wizard_id = fields.Many2one(
        'mint.pos.add.item.wizard', required=True, ondelete='cascade',
    )
    dutchie_product_id = fields.Char(string='Dutchie Product ID', required=True)
    name = fields.Char(string='Product')
    sku = fields.Char(string='SKU')
    brand = fields.Char(string='Brand')
    category = fields.Char(string='Category')
    price = fields.Float(string='Unit Price')

    def action_add_to_cart(self):
        self.ensure_one()
        wizard = self.wizard_id
        wizard._check_order_open()
        loc_id = wizard._get_loc_id()
        quantity = wizard.quantity or 1.0
        wizard._invsvc_post('/api/pos/add-item', {
            'locId': loc_id,
            'shipmentId': wizard.order_id.dutchie_shipment_id,
            'productId': self.dutchie_product_id,
            'productName': self.name or '',
            'unitPrice': self.price or 0,
            'quantity': quantity,
        })
        wizard.order_id.message_post(body=(
            'Added to POS cart: %s x%s ($%.2f) via Odoo'
            % (self.name or self.dutchie_product_id, int(quantity), self.price or 0)
        ))
        _logger.info(
            '[AddItem] order=%s shipment=%s product=%s qty=%s by uid=%s',
            wizard.order_id.name, wizard.order_id.dutchie_shipment_id,
            self.dutchie_product_id, quantity, self.env.uid,
        )
        return wizard._reopen()
