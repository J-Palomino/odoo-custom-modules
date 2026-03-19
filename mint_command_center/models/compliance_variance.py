from odoo import api, fields, models


class ComplianceVariance(models.Model):
    _name = 'mint.compliance.variance'
    _description = 'Compliance Variance — Regulatory approval tracker'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'submission_date desc, id desc'
    reference_unique = models.Constraint(
        'unique(reference)',
        'Variance reference must be unique.',
    )

    name = fields.Char(
        string='Name',
        compute='_compute_name',
        store=True,
    )
    reference = fields.Char(
        string='Reference',
        required=True,
        index=True,
        tracking=True,
        help='Internal tracking reference, e.g. VAR-FL-2026-001',
    )
    variance_type = fields.Selection(
        selection=[
            ('social_media', 'Social Media Registration'),
            ('paid_ads', 'Sponsored/Paid/Targeted Ads'),
            ('website_url', 'Website URL Approval'),
            ('brochure', 'Brochure / Printed Literature'),
            ('mobile_app', 'Mobile Application'),
            ('other', 'Other'),
        ],
        string='Type',
        required=True,
        tracking=True,
    )
    state = fields.Selection(
        selection=[
            ('draft', 'Draft'),
            ('submitted', 'Submitted'),
            ('under_review', 'Under Review'),
            ('approved', 'Approved'),
            ('denied', 'Denied'),
            ('expired', 'Expired'),
        ],
        string='Status',
        default='draft',
        required=True,
        tracking=True,
    )
    company_ids = fields.Many2many(
        'res.company',
        string='Stores',
        help='Stores this variance covers (can span multiple locations).',
    )
    state_id = fields.Many2one(
        'res.country.state',
        string='Jurisdiction',
        tracking=True,
        help='Regulatory jurisdiction (state).',
    )
    regulatory_body = fields.Char(
        string='Regulatory Body',
        help='e.g. FL OMMU, AZ DHS',
    )
    form_number = fields.Char(
        string='Form Number',
        help='e.g. DH5064-OMMU-12/2025',
    )
    rule_reference = fields.Char(
        string='Rule Reference',
        help='e.g. 64-4.023, F.A.C.',
    )

    # Dates
    submission_date = fields.Date(string='Submission Date', tracking=True)
    approval_date = fields.Date(string='Approval Date', tracking=True)
    expiration_date = fields.Date(string='Expiration Date', tracking=True)
    denial_date = fields.Date(string='Denial Date', tracking=True)

    denial_reason = fields.Text(string='Denial Reason')
    description = fields.Html(string='Description')
    notes = fields.Text(string='Internal Notes')

    # Computed
    is_expired = fields.Boolean(
        string='Expired',
        compute='_compute_is_expired',
        store=True,
    )
    days_until_expiry = fields.Integer(
        string='Days Until Expiry',
        compute='_compute_days_until_expiry',
    )

    # Platform fields (for social media / website types)
    platform_url = fields.Char(
        string='Platform URL',
        help='URL being approved (for website/social types).',
    )
    platform_name = fields.Char(
        string='Platform Name',
        help='Platform name (e.g. Instagram, X, TikTok).',
    )

    @api.depends('reference', 'variance_type')
    def _compute_name(self):
        type_labels = dict(self._fields['variance_type'].selection)
        for rec in self:
            label = type_labels.get(rec.variance_type, '')
            rec.name = f"{rec.reference or ''} — {label}".strip(' —')

    @api.depends('expiration_date')
    def _compute_is_expired(self):
        today = fields.Date.context_today(self)
        for rec in self:
            if rec.expiration_date:
                rec.is_expired = (rec.expiration_date - today).days < 0
            else:
                rec.is_expired = False

    @api.depends('expiration_date')
    def _compute_days_until_expiry(self):
        today = fields.Date.context_today(self)
        for rec in self:
            if rec.expiration_date:
                rec.days_until_expiry = (rec.expiration_date - today).days
            else:
                rec.days_until_expiry = 0
