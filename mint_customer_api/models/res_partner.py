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
