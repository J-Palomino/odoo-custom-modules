# -*- coding: utf-8 -*-
"""
Extend native pos.order to relay completed sales to Dutchie POS.

On payment confirmation, fires an async webhook to the inventory service
which enqueues a BullMQ job to push the sale through Dutchie's POS API
for METRC/BioTrack compliance tracking.
"""
import base64
import json
import logging
import threading

from odoo import api, fields, models
from odoo.exceptions import UserError

from . import zebra_zpl

_logger = logging.getLogger(__name__)

PRINTNODE_API_URL = 'https://api.printnode.com/printjobs'


class PosOrder(models.Model):
    _inherit = 'pos.order'

    # ── Dutchie sync fields ──────────────────────────────────────────
    dutchie_synced = fields.Boolean(
        string='Dutchie Synced',
        default=False,
        copy=False,
        help='True when this order has been successfully relayed to Dutchie POS.',
    )
    dutchie_receipt_no = fields.Char(
        string='Dutchie Receipt #',
        copy=False,
        index=True,
    )
    dutchie_shipment_id = fields.Char(
        string='Dutchie Shipment ID',
        copy=False,
    )
    dutchie_sync_error = fields.Text(
        string='Dutchie Sync Error',
        copy=False,
    )
    source = fields.Selection(
        [
            ('odoo_pos', 'Odoo POS'),
            ('dutchie_sync', 'Dutchie Sync'),
        ],
        string='Order Source',
        default='odoo_pos',
        copy=False,
    )

    # ── Relay logic ──────────────────────────────────────────────────

    @api.model
    def _process_order(self, order, draft, existing_order):
        """Override to fire Dutchie relay webhook after order is processed."""
        order_id = super()._process_order(order, draft, existing_order)

        if not draft:
            # Order is paid — relay to Dutchie in background thread
            pos_order = self.browse(order_id)
            if pos_order.exists():
                self._fire_dutchie_relay(pos_order)

        return order_id

    def _fire_dutchie_relay(self, order):
        """Send order data to inventory service for Dutchie POS relay.

        Runs in a background thread so POS UI isn't blocked.
        """
        config = order.session_id.config_id
        if not config.dutchie_enabled:
            _logger.info(
                'Dutchie relay disabled for POS config %s, skipping order %s',
                config.name, order.name,
            )
            return

        webhook_url = self.env['ir.config_parameter'].sudo().get_param(
            'mint_pos_dutchie.webhook_url',
            'https://mintinvsvc-production-6aa5.up.railway.app/api/webhook/pos-order-completed',
        )
        api_key = self.env['ir.config_parameter'].sudo().get_param(
            'mint_customer_api.checkout_api_key', '',
        )

        payload = self._build_relay_payload(order, config)

        # Fire-and-forget in background thread (don't block POS)
        def _post():
            import urllib.request
            try:
                data = json.dumps(payload).encode('utf-8')
                req = urllib.request.Request(
                    webhook_url,
                    data=data,
                    headers={
                        'Content-Type': 'application/json',
                        'X-Api-Key': api_key,
                    },
                    method='POST',
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    _logger.info(
                        'Dutchie relay webhook fired for order %s → %s',
                        order.name, resp.status,
                    )
            except Exception:
                _logger.exception(
                    'Failed to fire Dutchie relay webhook for order %s',
                    order.name,
                )

        thread = threading.Thread(target=_post, daemon=True)
        thread.start()

    def _build_relay_payload(self, order, config):
        """Build the webhook payload for the inventory service.

        Includes product_tmpl_id so the relay can look up the per-store
        dutchie_product_id from mint.product.location.
        """
        lines = []
        for line in order.lines:
            product = line.product_id
            tmpl = product.product_tmpl_id
            lines.append({
                'product_name': product.name,
                'product_tmpl_id': tmpl.id,
                'dutchie_product_id': tmpl.dutchie_product_id or '',
                'sku': product.default_code or '',
                'quantity': line.qty,
                'unit_price': line.price_unit,
                'discount': line.discount,
                'tax': line.price_subtotal_incl - line.price_subtotal,
                'category': tmpl.master_category or '',
                'brand': tmpl.brand_id.name if tmpl.brand_id else '',
                'strain_type': tmpl.strain_type or '',
            })

        customer = {}
        if order.partner_id:
            partner = order.partner_id
            customer = {
                'first_name': (partner.name or '').split(' ')[0],
                'last_name': ' '.join((partner.name or '').split(' ')[1:]) or '',
                'phone': partner.phone or partner.mobile or '',
                'email': partner.email or '',
            }

        # Map Odoo payment method to Dutchie
        payment_method = 'cash'
        for payment in order.payment_ids:
            pm_name = (payment.payment_method_id.name or '').lower()
            if 'debit' in pm_name:
                payment_method = 'debit'
                break
            elif 'card' in pm_name or 'credit' in pm_name:
                payment_method = 'card'
                break

        return {
            'odoo_order_id': order.id,
            'odoo_order_ref': order.name,
            'company_id': order.company_id.id,
            'store_slug': order.company_id.x_slug if hasattr(order.company_id, 'x_slug') else '',
            'session_id': order.session_id.id,
            'dutchie_loc_id': config.dutchie_loc_id,
            'dutchie_register_id': config.dutchie_register_id,
            'dutchie_room_id': config.dutchie_room_id,
            'items': lines,
            'customer': customer,
            'payment_method': payment_method,
            'totals': {
                'subtotal': order.amount_total - order.amount_tax,
                'taxes': order.amount_tax,
                'total': order.amount_total,
            },
            'source': 'odoo_pos',
        }

    # ── Manual retry ─────────────────────────────────────────────────

    def action_retry_dutchie_sync(self):
        """Manual retry button for failed Dutchie syncs."""
        for order in self.filtered(lambda o: not o.dutchie_synced):
            order.dutchie_sync_error = False
            config = order.session_id.config_id
            self._fire_dutchie_relay(order)

    # ── Zebra ZD410 printing ─────────────────────────────────────────

    def _mint_zebra_dpi(self):
        self.ensure_one()
        return int(self.session_id.config_id.mint_zebra_dpi or '203')

    def _mint_zebra_data(self):
        """Assemble a plain dict of receipt/label data from ORM fields.

        Built server-side from reliable fields so the ZPL output does not
        depend on version-sensitive POS JS getters.
        """
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
        """Frontend (WebUSB path) fetches ready-to-send ZPL by order ref."""
        order = self._mint_find_order(ref)
        if not order:
            return {'label': None, 'receipt': None, 'error': 'order_not_found'}
        return order._mint_zebra_zpl(which)

    @api.model
    def mint_printnode_print(self, ref, which='both'):
        """Server-side PrintNode proxy — keeps the API key off the browser.

        Posts base64-encoded ZPL to PrintNode for the register's configured
        printer id. Returns {'ok': bool, 'job_ids': [...], 'error': str}.
        """
        order = self._mint_find_order(ref)
        if not order:
            return {'ok': False, 'error': 'order_not_found'}

        config = order.session_id.config_id
        printer_id = config.mint_printnode_printer_id
        if not printer_id:
            return {'ok': False, 'error': 'printnode_printer_id_not_set'}

        api_key = self.env['ir.config_parameter'].sudo().get_param(
            'mint_pos_dutchie.printnode_api_key', '',
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

        import urllib.request
        import urllib.error

        auth = base64.b64encode(f'{api_key}:'.encode('utf-8')).decode('ascii')
        job_ids = []
        for title, content in jobs:
            payload = {
                'printerId': printer_id,
                'title': f'{title} {order.pos_reference or order.name}',
                'contentType': 'raw_base64',
                'content': base64.b64encode(content.encode('utf-8')).decode('ascii'),
                'source': 'Odoo POS (mint_pos_dutchie)',
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
                    # PrintNode returns the new print-job id as a bare integer.
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
