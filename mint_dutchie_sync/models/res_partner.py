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

    # --- Onboarding: 21+ verification (Odoo #101243) ---
    # Persisted at web signup from the ID scan. We store only the age-verified
    # flag + DOB — NOT the license number/image (PII-minimization; ticket
    # decision #3). The driver's-license fields from the earlier signup-v2
    # attempt (0bd7454) were removed as Odoo-19 schema casualties and are
    # intentionally not reintroduced.
    x_age_verified = fields.Boolean(
        string='Age Verified (21+)',
        help='Set when a scanned government ID confirmed the customer is 21 or older.',
    )
    x_date_of_birth = fields.Date(
        string='Date of Birth',
        help='Customer DOB captured at web signup (ID scan), used for 21+ verification.',
    )

    _dutchie_customer_id_unique = models.Constraint(
        'UNIQUE(x_dutchie_customer_id)',
        'Dutchie Customer ID must be unique.',
    )
