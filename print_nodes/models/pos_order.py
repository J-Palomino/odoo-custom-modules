# -*- coding: utf-8 -*-
"""Extend pos.order with Zebra ZPL generation + a server-side PrintNode proxy.

Part of the PrintNodes module. No Dutchie/inventory coupling — these methods
only build ZPL from the order and (optionally) relay it to PrintNode.
"""
import base64
import json
import logging
import urllib.error
import urllib.request

from odoo import api, fields, models

from . import zebra_zpl

_logger = logging.getLogger(__name__)
PRINTNODE_API_URL = 'https://api.printnode.com/printjobs'


class PosOrder(models.Model):
    _inherit = 'pos.order'

    def _mint_zebra_dpi(self):
        self.ensure_one()
        return int(self.session_id.config_id.mint_zebra_dpi or '203')

    def _mint_zebra_data(self):
        """Assemble a plain dict of receipt/label data from reliable ORM fields."""
        self.ensure_one()
        company = self.company_id
        currency = self.currency_id

        items = []
        for line in self.lines:
            items.append({
                'name': line.full_product_name or line.product_id.display_name,
                'qty': line.qty,
                'price': line.price_unit,
                'total': line.price_subtotal_incl,
            })

        address_parts = [company.street, company.city, company.state_id.code]
        address = ', '.join(p for p in address_parts if p)

        date_order = fields.Datetime.context_timestamp(self, self.date_order) \
            if self.date_order else None

        return {
            'store': company.name,
            'address': address,
            'order_ref': self.pos_reference or self.name,
            'barcode': ''.join(c for c in (self.pos_reference or self.name or '') if c.isdigit()) or (self.name or ''),
            'date': date_order.strftime('%Y-%m-%d %H:%M') if date_order else '',
            'cashier': self.employee_id.name or self.user_id.name or '',
            'customer': self.partner_id.name or '',
            'item_count': int(sum(self.lines.mapped('qty'))),
            'items': items,
            'subtotal': self.amount_total - self.amount_tax,
            'tax': self.amount_tax,
            'total': self.amount_total,
            'currency': currency.symbol or '$',
            'footer': self.session_id.config_id.receipt_footer or '',
        }

    def _mint_zebra_zpl(self, which='both'):
        """Return {'label': zpl|None, 'receipt': zpl|None} for this order."""
        self.ensure_one()
        data = self._mint_zebra_data()
        dpi = self._mint_zebra_dpi()
        out = {'label': None, 'receipt': None}
        if which in ('both', 'label'):
            out['label'] = zebra_zpl.build_label_zpl(data, dpi)
        if which in ('both', 'receipt'):
            out['receipt'] = zebra_zpl.build_receipt_zpl(data, dpi)
        return out

    @api.model
    def _mint_find_order(self, ref):
        """Resolve a POS order from a frontend reference (uuid / pos_reference / name)."""
        if not ref:
            return self.browse()
        domains = []
        if 'uuid' in self._fields:
            domains.append([('uuid', '=', ref)])
        domains.append([('pos_reference', '=', ref)])
        domains.append([('name', '=', ref)])
        for domain in domains:
            order = self.search(domain, limit=1)
            if order:
                return order
        return self.browse()

    @api.model
    def get_mint_zebra_zpl(self, ref, which='both'):
        """Frontend (WebUSB / local-agent path) fetches ready-to-send ZPL by ref."""
        order = self._mint_find_order(ref)
        if not order:
            return {'label': None, 'receipt': None, 'error': 'order_not_found'}
        return order._mint_zebra_zpl(which)

    @api.model
    def mint_printnode_print(self, ref, which='both'):
        """Server-side PrintNode proxy — keeps the API key off the browser."""
        order = self._mint_find_order(ref)
        if not order:
            return {'ok': False, 'error': 'order_not_found'}

        config = order.session_id.config_id
        printer_id = config.mint_printnode_printer_id
        if not printer_id:
            return {'ok': False, 'error': 'printnode_printer_id_not_set'}

        api_key = self.env['ir.config_parameter'].sudo().get_param(
            'print_nodes.printnode_api_key', '',
        )
        if not api_key:
            return {'ok': False, 'error': 'printnode_api_key_not_set'}

        zpl = order._mint_zebra_zpl(which)
        jobs = []
        if zpl.get('label'):
            jobs.append(('Exit Label', zpl['label']))
        if zpl.get('receipt'):
            jobs.append(('Receipt', zpl['receipt']))
        if not jobs:
            return {'ok': False, 'error': 'nothing_to_print'}

        auth = base64.b64encode(f'{api_key}:'.encode('utf-8')).decode('ascii')
        job_ids = []
        for title, content in jobs:
            payload = {
                'printerId': printer_id,
                'title': f'{title} {order.pos_reference or order.name}',
                'contentType': 'raw_base64',
                'content': base64.b64encode(content.encode('utf-8')).decode('ascii'),
                'source': 'Odoo POS (PrintNodes)',
            }
            try:
                req = urllib.request.Request(
                    PRINTNODE_API_URL,
                    data=json.dumps(payload).encode('utf-8'),
                    headers={
                        'Content-Type': 'application/json',
                        'Authorization': f'Basic {auth}',
                    },
                    method='POST',
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    body = resp.read().decode('utf-8')
                    job_ids.append(body.strip())
            except urllib.error.HTTPError as err:
                detail = err.read().decode('utf-8', 'replace')[:500]
                _logger.error('PrintNode print failed (%s) for %s: %s',
                              err.code, order.name, detail)
                return {'ok': False, 'error': f'printnode_http_{err.code}',
                        'detail': detail, 'job_ids': job_ids}
            except Exception as exc:  # noqa: BLE001
                _logger.exception('PrintNode print error for %s', order.name)
                return {'ok': False, 'error': str(exc), 'job_ids': job_ids}

        return {'ok': True, 'job_ids': job_ids}
