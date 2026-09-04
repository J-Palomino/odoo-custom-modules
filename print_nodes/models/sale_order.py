# -*- coding: utf-8 -*-
"""Store-receipt support for online (MintDeals) orders.

MintDeals online orders are sale.order records. This adds the data + a single
guarded entry point, mint_print_store_receipt(), that queues a receipt to the
order's store print node. The checkout controller (mint_customer_api) calls it
explicitly once the order and its lines exist, and again when payment is
marked - that is deterministic and, unlike a create()/write() override, never
fires for ordinary internal sale orders (x_checkout_status defaults to
'pending' on every sale.order, so an override could not tell them apart).

Routing is by company: a store with no print node is a safe no-op, so this
self-scopes to set-up stores. The cash drawer is not opened for an online order.
"""
from odoo import fields, models


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    x_receipt_printed = fields.Boolean(
        string='Store Receipt Printed', default=False, copy=False,
        help='Set once this order has been queued to its store print node, so '
             'it is not printed again (e.g. on a later pending -> paid update).')

    def _mint_autoprint_enabled(self):
        param = self.env['ir.config_parameter'].sudo().get_param(
            'print_nodes.auto_print_online', '1')
        return str(param).strip().lower() not in ('0', 'false', 'no', '')

    def _mint_receipt_data(self):
        """Receipt dict (same shape the POS receipt builders consume)."""
        self.ensure_one()
        company = self.company_id
        address = ', '.join(p for p in [
            company.street, company.city,
            company.state_id.code if company.state_id else None] if p)
        when = fields.Datetime.context_timestamp(self, self.date_order) \
            if self.date_order else None
        items = []
        for line in self.order_line:
            if line.display_type:
                continue
            items.append({
                'name': line.product_id.display_name or line.name,
                'qty': line.product_uom_qty,
                'price': line.price_unit,
                'total': line.price_total,
            })
        status = dict(self._fields['x_checkout_status'].selection).get(
            self.x_checkout_status, '') if 'x_checkout_status' in self._fields else ''
        return {
            'store': company.name,
            'address': address,
            'order_ref': self.name,
            'date': when.strftime('%Y-%m-%d %H:%M') if when else '',
            'cashier': 'Online Order',
            'customer': self.partner_id.name or '',
            'item_count': int(sum(self.order_line.filtered(
                lambda l: not l.display_type).mapped('product_uom_qty'))),
            'items': items,
            'subtotal': self.amount_untaxed,
            'tax': self.amount_tax,
            'total': self.amount_total,
            'currency': (self.currency_id.symbol or '$'),
            'footer': 'ONLINE ORDER - %s' % status if status else 'ONLINE ORDER',
        }

    def mint_print_store_receipt(self):
        """Queue a store receipt for each order here, once. Safe to call more
        than once (x_receipt_printed guards) and for any store (no node = no-op).
        Call it from the online-order flow once the order has its lines."""
        if not self._mint_autoprint_enabled():
            return
        Job = self.env['print.job'].sudo()
        for order in self:
            if order.x_receipt_printed:
                continue
            if not order.order_line.filtered(lambda l: not l.display_type):
                continue                     # no items yet - nothing to print
            res = Job.enqueue_receipt(
                order.company_id.id, order._mint_receipt_data(),
                order.name, open_drawer=False)
            if res.get('ok'):
                order.x_receipt_printed = True
