# -*- coding: utf-8 -*-
"""
Extend res.partner with Dutchie customer fields for daily sync.
"""
from odoo import fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    # Dutchie customer linkage
    x_dutchie_customer_id = fields.Char(
        string='Dutchie Customer ID',
        index=True,
        copy=False,
        help='Unique customer ID from Dutchie Backoffice (maintenance/get-customers).',
    )
    x_dutchie_customer_code = fields.Char(
        string='Dutchie Customer Code',
        index=True,
        copy=False,
        help='Customer code from Dutchie (e.g., state ID or patient number).',
    )

    # Home store assignment
    x_home_store_id = fields.Many2one(
        'res.company',
        string='Home Store',
        help='Primary dispensary location for this customer.',
    )
    x_home_store_source = fields.Selection(
        [
            ('portal', 'Portal signup'),
            ('dutchie', 'Dutchie sync'),
            ('manual', 'User chose later'),
        ],
        string='Home Store Source',
        copy=False,
        help='How the home store assignment was recorded. Only portal/manual '
             'values should hydrate the selected store on letsgomint.us login; '
             'dutchie-sourced values were inferred from a transaction and may '
             'not reflect a deliberate user choice.',
    )

    # State ID scan data (from signup-v2 flow)
    x_dl_number = fields.Char(
        string='Driver License Number',
        copy=False,
        groups='base.group_system',
        help='Parsed from PDF417 barcode on the back of a US state ID. '
             'Restricted to system group — PII.',
    )
    x_dl_state = fields.Char(
        string='Driver License State',
        size=2,
        copy=False,
        help='USPS 2-letter code of the state that issued the ID.',
    )
    x_dl_expiration = fields.Date(
        string='Driver License Expiration',
        copy=False,
    )
    x_dl_scanned_at = fields.Datetime(
        string='ID Scanned At',
        copy=False,
        help='When the ID was most recently scanned during signup or '
             'verification.',
    )

    # Sync metadata
    x_dutchie_last_sync = fields.Datetime(
        string='Last Dutchie Sync',
        copy=False,
        help='When this customer was last synced from Dutchie.',
    )
    x_dutchie_first_visit = fields.Date(
        string='First Visit',
        copy=False,
    )
    x_dutchie_total_spend = fields.Float(
        string='Total Spend (Dutchie)',
        digits=(12, 2),
        copy=False,
        help='Running total of net spend imported from Dutchie.',
    )
    x_dutchie_visit_count = fields.Integer(
        string='Visit Count',
        copy=False,
        help='Total number of transactions imported from Dutchie.',
    )

    # Purchase log (one2many)
    x_dutchie_purchase_ids = fields.One2many(
        'mint.dutchie.purchase',
        'partner_id',
        string='Dutchie Purchases',
    )

    _dutchie_customer_id_unique = models.Constraint(
        'UNIQUE(x_dutchie_customer_id)',
        'Dutchie Customer ID must be unique.',
    )
