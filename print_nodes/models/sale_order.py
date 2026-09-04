# -*- coding: utf-8 -*-
"""Auto-print an in-store receipt when an online order is paid or placed.

MintDeals online orders are sale.order records (see mint_customer_api): the
storefront creates them with x_checkout_status = 'pending' (pay online) or
'pay_at_store', and marks them 'paid' once payment clears. When an order becomes
a real, actionable order at a store, we queue a receipt to that store's print
node so staff see it and can fulfil it - the same node/printer pipeline the POS
uses, so it prints on whatever hardware the store has (Zebra label, Star/Epson
ESC/POS, or a raster Star driven via PDF).

Routing is by company: the order's company_id is the store, and print.node is
per company, so a Tempe order goes to the Tempe node. Stores without a
configured node simply do not print (enqueue_receipt is a safe no-op), which
scopes this to set-up stores automatically.

The cash drawer is NOT opened for an online order - there is no cash to take at
the counter for it.
"""
from odoo import api, fields, models

# Print as soon as an order comes in, regardless of payment: a 'pending'
# online order (placed, not yet paid) prints on creation, 'pay_at_store' prints
# on creation, and a pay-online order that lands already 'paid' prints then. The
# x_receipt_printed guard means each order prints once, so a later pending->paid
# transition does NOT reprint.
_PRINT_STATES = ('pending', 'paid', 'pay_at_store')


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    x_receipt_printed = fields.Boolean(
        string='Store Receipt Printed', default=False, copy=False,
        help='Set once this online order has been queued to its store print '
             'node, so it is not printed again on later edits.')

    def _mint_autoprint_enabled(self):
        param = self.env['ir.config_parameter'].sudo().get_param(
            'print_nodes.auto_print_online', '1')
        return str(param).strip().lower() not in ('0', 'false', 'no', '')

    def _mint_receipt_data(self):
        """Build the receipt dict (same shape the POS receipt builders consume)
        from this sale order."""
        self.ensure_one()
        company = self.company_id
        address = ', '.join(p for p in [
            company.street, company.city,
            company.state_id.code if company.state_id else None] if p)
        when = fields.Datetime.context_timestamp(self, self.date_order) \
            if self.date_order else None
        items = []
        for line in self.order_line:
            if line.display_type:            # section / note lines carry no product
                continue
            items.append({
                'name': line.product_id.display_name or line.name,
                'qty': line.product_uom_qty,
                'price': line.price_unit,
                'total': line.price_total,
            })
        status = dict(self._fields['x_checkout_status'].selection).get(
            self.x_checkout_status, '')
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

    def _mint_maybe_print_receipt(self):
        """Queue a store receipt for any order here that just became printable."""
        if not self._mint_autoprint_enabled():
            return
        Job = self.env['print.job'].sudo()
        for order in self:
            if order.x_receipt_printed:
                continue
            if order.x_checkout_status not in _PRINT_STATES:
                continue
            if not order.order_line.filtered(lambda l: not l.display_type):
                continue                     # nothing to print
            res = Job.enqueue_receipt(
                order.company_id.id, order._mint_receipt_data(),
                order.name, open_drawer=False)
            if res.get('ok'):
                # direct write of the flag only - keeps out of the trigger below
                order.x_receipt_printed = True

    @api.model_create_multi
    def create(self, vals_list):
        orders = super().create(vals_list)
        # pay-at-store orders are born already in a printable state
        orders._mint_maybe_print_receipt()
        return orders

    def write(self, vals):
        res = super().write(vals)
        if 'x_checkout_status' in vals:      # e.g. pending -> paid
            self._mint_maybe_print_receipt()
        return res
