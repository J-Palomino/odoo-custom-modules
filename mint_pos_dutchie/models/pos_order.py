# -*- coding: utf-8 -*-
"""
Extend native pos.order to relay completed sales to Dutchie POS.

On payment confirmation, fires an async webhook to the inventory service
which enqueues a BullMQ job to push the sale through Dutchie's POS API
for METRC/BioTrack compliance tracking.
"""
import json
import logging
import threading

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


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
