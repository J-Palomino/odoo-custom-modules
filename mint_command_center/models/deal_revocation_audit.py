from odoo import fields, models


class DealRevocationAuditLog(models.Model):
    _name = 'mint.deal.revocation.audit.log'
    _description = 'Audit log entry for a PTL deal revocation action'
    _order = 'create_date desc, id desc'
    _rec_name = 'display_name'

    deal_id = fields.Many2one(
        'mint.ptl.deal',
        string='PTL Deal',
        ondelete='set null',
        index=True,
    )
    submission_id = fields.Many2one(
        'mint.deal.submission',
        string='Source Submission',
        ondelete='set null',
        index=True,
    )
    scope = fields.Selection(
        selection=[
            ('single_day', 'Single Day'),
            ('from_date_forward', 'From Date Forward'),
            ('all_future_with_requeue', 'All Future + Re-queue Submission'),
            ('all_instances', 'All Instances'),
        ],
        string='Scope',
        required=True,
    )
    from_date = fields.Date(string='From Date (if scoped)')
    affected_day_ids_json = fields.Text(
        string='Affected PTL Day IDs (JSON)',
        help='JSON array of mint.ptl.day record IDs whose deal_ids relation '
             'was severed. Stored as text to survive ondelete cascades.',
    )
    affected_discount_ids_json = fields.Text(
        string='Affected Discount IDs (JSON)',
        help='JSON array of mint.discount IDs whose is_published / valid_until '
             'was rescoped or deactivated as a result of this revoke.',
    )
    requeued_submission = fields.Boolean(
        string='Submission Re-queued',
        default=False,
        help='True when scope=all_future_with_requeue cloned the source '
             'submission back to state=new for re-review.',
    )
    user_id = fields.Many2one(
        'res.users',
        string='Performed By',
        required=True,
        default=lambda self: self.env.user,
    )
    reason = fields.Text(string='Reason', required=True)

    display_name = fields.Char(
        string='Reference',
        compute='_compute_display_name',
        store=True,
    )

    @staticmethod
    def _scope_label_map():
        return {
            'single_day': 'Single Day',
            'from_date_forward': 'From Date Forward',
            'all_future_with_requeue': 'All Future + Requeue',
            'all_instances': 'All Instances',
        }

    def _compute_display_name(self):
        labels = self._scope_label_map()
        for rec in self:
            deal = rec.deal_id.name if rec.deal_id else f"deal#{rec.deal_id.id or '?'}"
            rec.display_name = f"Revoke({labels.get(rec.scope, rec.scope)}): {deal}"
