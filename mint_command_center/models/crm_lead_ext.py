from odoo import fields, models


class CrmLead(models.Model):
    _inherit = 'crm.lead'

    vendor_brand_id = fields.Many2one(
        'mint.brand',
        string='Vendor Brand',
        index=True,
        tracking=True,
        help='Brand this vendor lead represents.',
    )
    vendor_funding_amount = fields.Monetary(
        string='Proposed Vendor Funding',
        currency_field='vendor_funding_currency_id',
        tracking=True,
    )
    vendor_funding_percent = fields.Float(string='Proposed Funding %', tracking=True)
    vendor_funding_currency_id = fields.Many2one(
        'res.currency',
        string='Funding Currency',
        default=lambda self: self.env.company.currency_id,
    )
    campaign_ids = fields.One2many(
        'mint.national.promo',
        'crm_lead_id',
        string='Promo Campaigns',
    )
    submission_ids = fields.One2many(
        'mint.deal.submission',
        'crm_lead_id',
        string='Deal Submissions',
    )

    def action_create_submission(self):
        """Open a pre-filled mint.deal.submission form for this lead."""
        self.ensure_one()
        Submission = self.env['mint.deal.submission']
        context = {
            'default_crm_lead_id': self.id,
            'default_vendor_name': (
                self.partner_id.name if self.partner_id else self.contact_name or self.name
            ),
            'default_vendor_email': self.email_from or '',
            'default_vendor_contact': self.contact_name or '',
            'default_vendor_phone': self.phone or '',
            'default_brand_id': self.vendor_brand_id.id if self.vendor_brand_id else False,
            'default_vendor_funding_amount': self.vendor_funding_amount or 0.0,
            'default_vendor_funding_percent': self.vendor_funding_percent or 0.0,
        }
        return {
            'name': 'New Deal Submission',
            'type': 'ir.actions.act_window',
            'res_model': 'mint.deal.submission',
            'view_mode': 'form',
            'target': 'current',
            'context': context,
        }
