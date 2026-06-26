# -*- coding: utf-8 -*-
from odoo import models, fields


class ResPartner(models.Model):
    _inherit = 'res.partner'

    is_web_customer = fields.Boolean(
        string='Web Customer',
        default=False,
        index=True,
        help='Created via mintdeals.com / shop.letsgomint.us signup. '
             'Record rules restrict visibility to privileged users only.',
    )

    # Links a web-customer contact back to the internal staff user it belongs
    # to, when an employee signs into the storefront with their company
    # identity. "Linked but separate": this is a distinct customer res.partner
    # (its own order history + PII rules), pointing at the staff res.users.
    # Null for ordinary consumers. Set by the SSO employee-recognition path.
    employee_user_id = fields.Many2one(
        'res.users',
        string='Linked Staff User',
        index=True,
        ondelete='set null',
        help='If this web customer belongs to an employee, the internal '
             'staff user account it is linked to. Distinct from this contact.',
    )
