# -*- coding: utf-8 -*-
"""
Dutchie purchase log — one record per transaction imported.
Prevents duplicate imports and provides a browsable purchase history.
"""
from odoo import fields, models


class DutchiePurchase(models.Model):
    _name = 'mint.dutchie.purchase'
    _description = 'Dutchie Purchase Record'
    _order = 'date desc, id desc'
    _rec_name = 'receipt_no'

    partner_id = fields.Many2one(
        'res.partner',
        string='Customer',
        required=True,
        ondelete='cascade',
        index=True,
    )
    company_id = fields.Many2one(
        'res.company',
        string='Store',
        required=True,
        index=True,
    )

    # Transaction identifiers
    receipt_no = fields.Char(string='Receipt #', index=True)
    date = fields.Date(string='Date', required=True, index=True)

    # Financials
    gross_total = fields.Float(string='Gross Total', digits=(12, 2))
    discount = fields.Float(string='Discount', digits=(12, 2))
    net_total = fields.Float(string='Net Total', digits=(12, 2))
    tax = fields.Float(string='Tax', digits=(12, 2))

    # Points awarded
    loyalty_points = fields.Float(string='Points Awarded', digits=(12, 2))

    # Line items stored as text (JSON) for reference without needing a third model
    line_items_json = fields.Text(
        string='Line Items (JSON)',
        help='JSON array of {product, sku, qty, price} from Dutchie dispensations report.',
    )

    _receipt_unique = models.Constraint(
        'UNIQUE(receipt_no, company_id)',
        'Receipt number must be unique per store.',
    )
