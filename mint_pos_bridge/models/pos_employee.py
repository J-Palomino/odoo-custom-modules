# -*- coding: utf-8 -*-
import logging
from odoo import api, fields, models

_logger = logging.getLogger(__name__)

ACCESS_LEVELS = [
    ('budtender', 'Budtender'),
    ('manager', 'Manager'),
    ('admin', 'Admin'),
]


class MintPosEmployeeCredential(models.Model):
    _name = 'mint.pos.employee.credential'
    _description = 'POS Employee Credential'
    _order = 'employee_id, company_id'

    employee_id = fields.Many2one(
        'hr.employee',
        string='Employee',
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
    access_level = fields.Selection(
        selection=ACCESS_LEVELS,
        string='Access Level',
        default='budtender',
        required=True,
    )
    is_service_account = fields.Boolean(
        string='Service Account',
        default=True,
        help='If checked, uses the store API key instead of named credentials.',
    )

    # Named Dutchie credentials (managers only)
    dutchie_username = fields.Char(
        string='Dutchie Username',
        groups='mint_pos_bridge.group_pos_manager',
    )
    dutchie_credential = fields.Char(
        string='Dutchie Password',
        groups='mint_pos_bridge.group_pos_manager',
        help='Stored encrypted. Only visible to POS Managers.',
    )
    dutchie_session_id = fields.Char(
        string='Session ID',
        groups='mint_pos_bridge.group_pos_manager',
        copy=False,
    )
    session_expires_at = fields.Datetime(
        string='Session Expires',
        copy=False,
    )

    active = fields.Boolean(default=True)

    _sql_constraints = [
        ('employee_company_uniq', 'UNIQUE(employee_id, company_id)',
         'An employee can only have one credential per store.'),
    ]

    @api.onchange('access_level')
    def _onchange_access_level(self):
        if self.access_level == 'budtender':
            self.is_service_account = True
            self.dutchie_username = False
            self.dutchie_credential = False

    def is_session_valid(self):
        """Check if the cached Dutchie session is still valid."""
        self.ensure_one()
        if not self.dutchie_session_id or not self.session_expires_at:
            return False
        return fields.Datetime.now() < self.session_expires_at
