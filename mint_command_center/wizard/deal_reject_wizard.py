from odoo import fields, models


class DealRejectWizard(models.TransientModel):
    _name = 'mint.deal.reject.wizard'
    _description = 'Deal Rejection Reason'

    reason = fields.Text(string='Rejection Reason', required=True)

    def action_confirm(self):
        active_model = self.env.context.get('active_model')
        active_ids = self.env.context.get('active_ids', [])
        if not active_model or not active_ids:
            return
        records = self.env[active_model].browse(active_ids)
        records.write({
            'state': 'rejected',
            'rejection_reason': self.reason,
            'reviewed_by': self.env.uid,
            'reviewed_at': fields.Datetime.now(),
        })
