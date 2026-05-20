from datetime import date as _date

from odoo import api, fields, models
from odoo.exceptions import UserError


class ForceApproveWizard(models.TransientModel):
    _name = 'mint.deal.force.approve.wizard'
    _description = 'Force Approve & Plot — override the campaign-approval gate'

    submission_id = fields.Many2one(
        'mint.deal.submission',
        string='Submission',
        required=True,
        readonly=True,
    )
    campaign_id = fields.Many2one(
        related='submission_id.campaign_id',
        string='Linked Campaign',
        readonly=True,
    )
    campaign_state = fields.Selection(
        related='campaign_id.state',
        readonly=True,
    )
    also_approve_campaign = fields.Boolean(
        string='Also approve the campaign now',
        default=True,
        help='If checked, find-or-create the National Promo Campaign for '
             'this brand × market × year and set its state to approved '
             'as part of this override. Leave unchecked if you only want '
             'to plot this one submission without blessing the campaign.',
    )
    reason = fields.Text(
        string='Override Reason',
        required=True,
        help='Required. Will be written to chatter on both the submission '
             'and the campaign for audit. Be specific — "Vendor pre-paid, '
             'paperwork pending" is useful, "ok" is not.',
    )

    def action_apply(self):
        self.ensure_one()
        if not self.reason or not self.reason.strip():
            raise UserError("Override reason is required.")

        sub = self.submission_id
        if not sub:
            raise UserError("No submission linked to this wizard.")

        campaign = sub.campaign_id

        # Find-or-create the campaign if missing
        if not campaign and self.also_approve_campaign:
            if not sub.brand_id or not sub.market_id:
                raise UserError(
                    "Cannot create a campaign — submission is missing "
                    "brand or market. Fill those first or uncheck "
                    "\"Also approve the campaign\"."
                )
            year = (
                sub.preferred_start_date.year
                if sub.preferred_start_date
                else _date.today().year
            )
            campaign = self.env['mint.national.promo'].sudo().get_or_create(
                brand_id=sub.brand_id.id,
                market_id=sub.market_id.id,
                year=year,
                crm_lead_id=sub.crm_lead_id.id if sub.crm_lead_id else False,
            )
            sub.campaign_id = campaign.id

        # Approve the campaign if asked
        if campaign and self.also_approve_campaign and campaign.state == 'planning':
            campaign.write({'state': 'approved'})

        body = (
            f"<p><b>Force Approve &amp; Plot</b> override applied by "
            f"<b>{self.env.user.name}</b>.</p>"
            f"<p><b>Reason:</b> {self.reason}</p>"
        )
        sub.message_post(body=body, message_type='comment')
        if campaign:
            campaign.message_post(
                body=(
                    f"{body}"
                    f"<p>Plotted submission "
                    f"<b>SUB-{sub.id:05d}</b>: {sub.name}</p>"
                ),
                message_type='comment',
            )

        # Now plot — bypass the gate via context
        return sub.with_context(force_override=True).action_convert_to_deal()
