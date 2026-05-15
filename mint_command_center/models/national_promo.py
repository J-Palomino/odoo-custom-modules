from datetime import date

from odoo import api, fields, models
from odoo.exceptions import UserError


class NationalPromo(models.Model):
    _name = 'mint.national.promo'
    _description = 'National Promo Campaign — brand × market × year'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'year desc, brand_id, market_id'

    brand_id = fields.Many2one(
        'mint.brand',
        string='Brand',
        required=True,
        index=True,
        tracking=True,
    )
    market_id = fields.Many2one(
        'mint.region',
        string='Market',
        required=True,
        index=True,
        tracking=True,
    )
    year = fields.Integer(
        string='Year',
        required=True,
        index=True,
        default=lambda self: date.today().year,
        tracking=True,
    )
    name = fields.Char(
        string='Name',
        compute='_compute_name',
        store=True,
    )

    # CRM / negotiation thread
    crm_lead_id = fields.Many2one(
        'crm.lead',
        string='CRM Lead',
        ondelete='set null',
        tracking=True,
    )

    # Linked records
    submission_ids = fields.One2many(
        'mint.deal.submission',
        'campaign_id',
        string='Vendor Submissions',
    )
    entry_ids = fields.One2many(
        'mint.brand.calendar.entry',
        'campaign_id',
        string='Calendar Entries',
    )
    entry_count = fields.Integer(
        string='Entry Count',
        compute='_compute_entry_count',
        store=True,
    )
    submission_count = fields.Integer(
        string='Submission Count',
        compute='_compute_submission_count',
        store=True,
    )

    # Outreach / plotting status (mirrors Brands.xlsx)
    contact_status = fields.Selection(
        selection=[
            ('not_contacted', 'Not Contacted'),
            ('outreached', 'Outreach Sent'),
            ('awaiting_response', 'Waiting for Answer'),
            ('confirmed', 'Got Confirmation'),
            ('declined', 'Declined'),
        ],
        string='Outreach Status',
        default='not_contacted',
        tracking=True,
    )
    ready_to_plot = fields.Boolean(string='Ready to Plot', tracking=True)
    plotted = fields.Boolean(string='Plotted', tracking=True)

    # Vendor funding contract
    vendor_funding_total = fields.Monetary(
        string='Vendor Funding (Total)',
        currency_field='vendor_funding_currency_id',
        tracking=True,
    )
    vendor_funding_currency_id = fields.Many2one(
        'res.currency',
        string='Funding Currency',
        default=lambda self: self.env.company.currency_id,
    )
    vendor_funding_terms = fields.Text(string='Funding Terms')

    # Campaign state
    state = fields.Selection(
        selection=[
            ('planning', 'Planning'),
            ('approved', 'Approved'),
            ('active', 'Active'),
            ('closed', 'Closed'),
        ],
        string='Status',
        default='planning',
        tracking=True,
    )

    # Backfill provenance
    source_file = fields.Char(
        string='Source File',
        help='Relative path of the XLSX this campaign was imported from.',
        readonly=True,
    )
    notes = fields.Text(string='Notes')

    _sql_constraints = [
        ('brand_market_year_uniq',
         'unique(brand_id, market_id, year)',
         'Only one national promo per brand per market per year.'),
    ]

    @api.depends('brand_id', 'market_id', 'year')
    def _compute_name(self):
        for rec in self:
            brand = rec.brand_id.name or 'New Brand'
            code = rec.market_id.code or rec.market_id.name or ''
            suffix = f" [{code}]" if code else ''
            rec.name = f"{brand} {rec.year}{suffix}" if rec.year else f"{brand}{suffix}"

    @api.depends('entry_ids')
    def _compute_entry_count(self):
        for rec in self:
            rec.entry_count = len(rec.entry_ids)

    @api.depends('submission_ids')
    def _compute_submission_count(self):
        for rec in self:
            rec.submission_count = len(rec.submission_ids)

    # ── State actions ─────────────────────────────────────────────────────

    def action_start_planning(self):
        self.filtered(lambda c: c.state != 'planning').write({'state': 'planning'})

    def action_approve(self):
        self.filtered(lambda c: c.state == 'planning').write({'state': 'approved'})

    def action_activate(self):
        self.filtered(lambda c: c.state in ('planning', 'approved')).write({'state': 'active'})

    def action_close(self):
        self.write({'state': 'closed'})

    # ── CRM linkage ────────────────────────────────────────────────────────

    def action_open_lead(self):
        self.ensure_one()
        if not self.crm_lead_id:
            raise UserError("No CRM lead linked to this campaign yet.")
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'crm.lead',
            'res_id': self.crm_lead_id.id,
            'view_mode': 'form',
        }

    def action_create_lead(self):
        self.ensure_one()
        if self.crm_lead_id:
            return self.action_open_lead()
        lead = self.env['crm.lead'].create({
            'name': f"{self.brand_id.name} — {self.year} Promo Campaign",
            'type': 'opportunity',
            'vendor_brand_id': self.brand_id.id,
        })
        self.write({'crm_lead_id': lead.id})
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'crm.lead',
            'res_id': lead.id,
            'view_mode': 'form',
        }

    # ── Find-or-create helper for use by submission/backfill ──────────────

    @api.model
    def get_or_create(self, brand_id, market_id, year, **defaults):
        """Idempotent find-or-create for brand × market × year campaigns."""
        if not brand_id or not market_id or not year:
            return self.browse()
        existing = self.search([
            ('brand_id', '=', brand_id),
            ('market_id', '=', market_id),
            ('year', '=', year),
        ], limit=1)
        if existing:
            return existing
        vals = {'brand_id': brand_id, 'market_id': market_id, 'year': year}
        vals.update(defaults)
        return self.create(vals)
