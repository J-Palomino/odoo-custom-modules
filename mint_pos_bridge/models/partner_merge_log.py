# -*- coding: utf-8 -*-
"""
Audit log for partner merges (Phase 6, issue #278).

Merged source partners are DELETED by base.partner.merge.automatic.wizard,
so chatter on the survivor is not enough to reconstruct what happened.
Every merge (and every dry-run that WOULD have merged) writes a row here.
"""
from odoo import fields, models


class PartnerMergeLog(models.Model):
    _name = 'mint.partner.merge.log'
    _description = 'Partner Merge Audit Log'
    _order = 'merged_at desc'

    dst_partner_id = fields.Many2one(
        'res.partner',
        string='Merged Into',
        ondelete='set null',
        index=True,
    )
    src_partner_desc = fields.Char(
        string='Source Partner',
        help='Snapshot of the deleted source: name, id, origin, strong keys.',
    )
    matched_key = fields.Selection(
        [
            ('customer_id', 'Dutchie Customer ID'),
            ('loyalty_id', 'Dutchie Loyalty ID'),
            ('identity_key', 'Identity Key'),
            ('phone', 'Phone'),
            ('email', 'Email'),
            ('manual', 'Manual'),
        ],
        string='Matched On',
    )
    mode = fields.Selection(
        [
            ('auto', 'Auto (cron)'),
            ('manual', 'Manual'),
            ('dry_run', 'Dry Run'),
        ],
        string='Mode',
        default='auto',
    )
    merged_at = fields.Datetime(
        string='Merged At',
        default=fields.Datetime.now,
        index=True,
    )
    notes = fields.Char(string='Notes')
