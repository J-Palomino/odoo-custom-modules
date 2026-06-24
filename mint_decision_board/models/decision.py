import re

from odoo import api, fields, models


def slugify(value):
    """Lowercase ASCII slug — no external deps."""
    value = (value or '').strip().lower()
    value = re.sub(r'[^a-z0-9]+', '-', value)
    return value.strip('-') or 'decision'


class MintDecision(models.Model):
    _name = 'mint.decision'
    _description = 'Decision Board'
    _order = 'sequence, id'

    name = fields.Char(string='Title', required=True)
    slug = fields.Char(
        string='URL Slug', required=True, copy=False, index=True,
        help='Used in the public URL: /decision/<slug>.')
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    # Branded masthead bits — all optional, fall back gracefully.
    kicker = fields.Char(string='Eyebrow', help='Small uppercase label above the title.')
    subtitle = fields.Char(string='Subtitle')
    question = fields.Char(string='Framed Question')

    # The editable rich content shown between the masthead and the ballot.
    body_html = fields.Html(string='Body', sanitize=False)

    # Ballot configuration.
    ballot_heading = fields.Char(string='Ballot Heading', default='Cast Your Vote')
    choice_label = fields.Char(string='Choice Label', default='Your verdict')
    allow_comment = fields.Boolean(string='Collect Comment', default=True)
    comment_label = fields.Char(string='Comment Label', default='Reasoning / input')
    comment_placeholder = fields.Char(
        string='Comment Placeholder',
        default="What's driving your call? Conditions, concerns, or a different path…")
    takeaway = fields.Text(string='Footer Takeaway')

    state = fields.Selection(
        [('draft', 'Draft'), ('open', 'Open'), ('closed', 'Closed')],
        default='draft', required=True,
        help='Draft = hidden; Open = accepting votes; Closed = results only.')
    audience = fields.Selection(
        [('all', 'All internal users'), ('csuite', 'C-Suite only')],
        default='all', required=True,
        help='Who may open and vote on this board. C-Suite = the allow-list '
             'in System Parameter mint_decision_board.csuite_logins.')

    option_ids = fields.One2many('mint.decision.option', 'decision_id', string='Options')
    vote_ids = fields.One2many('mint.decision.vote', 'decision_id', string='Votes')
    vote_count = fields.Integer(compute='_compute_vote_count')

    _sql_constraints = [
        ('slug_uniq', 'unique(slug)', 'The URL slug must be unique.'),
    ]

    @api.depends('vote_ids')
    def _compute_vote_count(self):
        for rec in self:
            rec.vote_count = len(rec.vote_ids)

    # ------------------------------------------------------------------
    # C-Suite allow-list (editable System Parameter, not hardcoded).
    # ------------------------------------------------------------------
    @api.model
    def _csuite_logins(self):
        raw = self.env['ir.config_parameter'].sudo().get_param(
            'mint_decision_board.csuite_logins', '')
        return {p.strip().lower() for p in raw.split(',') if p.strip()}

    @api.model
    def _is_csuite(self, user=None):
        """True if `user` (default: current) is on the C-Suite allow-list."""
        user = user or self.env.user
        logins = self._csuite_logins()
        if not logins:
            return False
        return ((user.login or '').lower() in logins
                or (user.email or '').lower() in logins)

    def _user_can_view(self, user):
        """Per-board audience check (audience='csuite' honours the allow-list)."""
        self.ensure_one()
        if self.audience == 'csuite':
            return self._is_csuite(user)
        return True

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('slug'):
                vals['slug'] = self._unique_slug(slugify(vals.get('name')))
        return super().create(vals_list)

    def write(self, vals):
        if vals.get('slug'):
            vals['slug'] = self._unique_slug(slugify(vals['slug']), exclude=self.ids)
        return super().write(vals)

    def _unique_slug(self, base, exclude=None):
        base = base or 'decision'
        candidate, n = base, 1
        domain = [('slug', '=', candidate)]
        if exclude:
            domain.append(('id', 'not in', list(exclude)))
        while self.sudo().search_count(domain):
            n += 1
            candidate = f'{base}-{n}'
            domain = [('slug', '=', candidate)]
            if exclude:
                domain.append(('id', 'not in', list(exclude)))
        return candidate

    def get_tally(self):
        """Ordered option tally with vote counts and integer percentages."""
        self.ensure_one()
        total = len(self.vote_ids)
        rows = []
        for opt in self.option_ids.sorted(lambda o: (o.sequence, o.id)):
            count = len(opt.vote_ids)
            rows.append({
                'option': opt,
                'label': opt.name,
                'count': count,
                'percent': 0 if not total else round(100 * count / total),
            })
        return rows


class MintDecisionOption(models.Model):
    _name = 'mint.decision.option'
    _description = 'Decision Option'
    _order = 'sequence, id'

    decision_id = fields.Many2one(
        'mint.decision', required=True, ondelete='cascade', index=True)
    name = fields.Char(string='Label', required=True)
    description = fields.Char(string='Sub-text')
    sequence = fields.Integer(default=10)
    vote_ids = fields.One2many('mint.decision.vote', 'option_id', string='Votes')
    vote_count = fields.Integer(compute='_compute_vote_count')

    @api.depends('vote_ids')
    def _compute_vote_count(self):
        for rec in self:
            rec.vote_count = len(rec.vote_ids)
