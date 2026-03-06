# -*- coding: utf-8 -*-
from odoo import fields, models


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    x_dutchie_checkout_id = fields.Char(
        string='Dutchie Checkout ID',
        index=True,
        help='UUID from Dutchie Plus API checkout session',
    )
    x_payment_method = fields.Selection(
        [('online', 'Pay Online'), ('in-store', 'Pay at Store')],
        string='Payment Method',
        default='online',
    )
    x_checkout_status = fields.Selection(
        [
            ('pending', 'Pending Payment'),
            ('paid', 'Paid'),
            ('pay_at_store', 'Pay at Store'),
            ('cancelled', 'Cancelled'),
        ],
        string='Checkout Status',
        default='pending',
        tracking=True,
    )
