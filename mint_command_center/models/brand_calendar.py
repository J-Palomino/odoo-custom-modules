import re

from odoo import api, fields, models
from odoo.exceptions import UserError

from .deal_mixins import DISCOUNT_TYPE_SELECTION


# Extracts cannabis-weight metadata from a free-text title.
# Two-pass: prefer mass units (mg/g/oz) over count units (pk/ct) so titles like
# "Shorties Prerolls 10pk - (5.0g)" return 5.0g (the gram weight), not 10ct.
# `mg` is listed before `g` so "100mg" parses as mg, not g. `pk` → `ct`.
_WEIGHT_RE_MASS = re.compile(r'\b(\d*\.?\d+)\s*(mg|g|oz)\b', re.IGNORECASE)
_WEIGHT_RE_COUNT = re.compile(r'\b(\d*\.?\d+)\s*(pk|ct)\b', re.IGNORECASE)


# Normalize a brand name for case/punctuation/whitespace-insensitive lookup.
# Used by _compute_brand_id on mint.ptl.deal to avoid creating dupes like
# "Cresco" when "Cresco " (trailing space) already exists, or "Tru Infusion"
# when "TRU-Infusion" already exists. Matches the dedupe script's `norm()`.
_BRAND_NORM_RE = re.compile(r"[\s\-_.&,'\"!?()]+")


def _brand_lookup_key(name):
    if not name:
        return ''
    return _BRAND_NORM_RE.sub('', str(name).strip().lower())


# Common separators used between "Brand" and the rest of a deal title.
# Verified against 4.1k production deal names; ` - ` and ` · ` cover the vast
# majority. `: ` is rare but used (e.g. "Free Tacos: Sponsored by ...").
_BRAND_SEPARATORS = [
    re.compile(r'\s+-\s+'),
    re.compile(r'\s*·\s*'),
    re.compile(r'\s+\|\s+'),
    re.compile(r':\s+'),
]


def _looks_like_promo(s):
    """Heuristics to reject promo-math fragments masquerading as brand names.
    Catches '2 for $59 3 for $39', '30% Off', '$25 Each', etc."""
    if not s:
        return True
    if '$' in s or '%' in s:
        return True
    if re.search(r'\b\d+\s+for\s+\$?\d', s, re.IGNORECASE):
        return True
    if re.search(r'\boff\b', s, re.IGNORECASE):
        return True
    if re.fullmatch(r'\d+(\.\d+)?', s):
        return True
    return False


def _parse_brand_name(text):
    """Extract the brand-name segment from a deal title.

    Returns the trimmed string before the first separator, or None when no
    plausible brand is found. Junk like promo math is filtered out.
    Examples:
        "Camino - Gummies 100mg" → "Camino"
        "Sofa King · Bite Me Brownies"  → "Sofa King"
        "DOUBLE FREE WEED FRIDAY" → None  (no separator)
        "2 for $59 - some product" → None  (promo-math leader)
    """
    if not text:
        return None
    s = str(text).strip()
    if not s:
        return None
    for sep_re in _BRAND_SEPARATORS:
        m = sep_re.search(s)
        if not m or m.start() == 0:
            continue
        candidate = s[:m.start()].strip()
        if len(candidate) < 2 or len(candidate) > 50:
            continue
        if _looks_like_promo(candidate):
            continue
        return candidate
    return None


def _parse_weight(*sources):
    """Try each source string in order; return (value:float, unit:str) or (0.0, False).
    Mass units win over count units within the same string."""
    for s in sources:
        if not s:
            continue
        m = _WEIGHT_RE_MASS.search(s) or _WEIGHT_RE_COUNT.search(s)
        if not m:
            continue
        try:
            value = float(m.group(1))
        except (TypeError, ValueError):
            continue
        unit = m.group(2).lower()
        if unit == 'pk':
            unit = 'ct'
        return value, unit
    return 0.0, False


class BrandCalendarEntry(models.Model):
    _name = 'mint.brand.calendar.entry'
    _description = 'Brand Calendar Entry — Scheduled brand promotion slot'
    _inherit = [
        'mail.thread',
        'mint.discount.core.mixin', 'mint.vendor.funding.mixin',
        'mint.weight.parsed.mixin',
    ]
    _order = 'date, brand_id'

    # ── Identity ──────────────────────────────────────────────────────────

    brand_id = fields.Many2one(
        'mint.brand',
        string='Brand',
        required=True,
        index=True,
        tracking=True,
    )
    date = fields.Date(
        string='Date',
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
    campaign_id = fields.Many2one(
        'mint.national.promo',
        string='Campaign',
        ondelete='set null',
        index=True,
        tracking=True,
    )

    # ── SKU / product targeting ───────────────────────────────────────────

    sku_or_category = fields.Char(
        string='SKU / Category',
        tracking=True,
        help='Raw column header from the National Promo XLSX (e.g. "LR Cannons Prerolls", "1g AIO"). '
             'Free-text by design; resolves async to product_template_id or product_category_id.',
    )
    product_template_id = fields.Many2one(
        'product.template',
        string='Product Template',
        index=True,
        help='Optional precise product link, once sku_or_category is resolved.',
    )
    product_category_id = fields.Many2one(
        'product.category',
        string='Product Category',
        index=True,
        help='Alternative when the header maps to a category rather than a single SKU.',
    )

    # ── Promo math ────────────────────────────────────────────────────────

    promo_text = fields.Char(
        string='Promo Text',
        tracking=True,
        help='Raw XLSX cell content (e.g. "40% off", "2 for $59").',
    )
    # discount_type / discount_value / original_price come from
    # mint.discount.core.mixin; brand calendar tracks all three. The selection
    # is re-passed explicitly (shared constant) so the override can't drop it.
    discount_type = fields.Selection(DISCOUNT_TYPE_SELECTION, tracking=True)
    discount_value = fields.Float(tracking=True)
    original_price = fields.Float(tracking=True)

    # ── Weight metadata (parsed from sku_or_category / promo_text) ───────
    # Stored compute with readonly=False: auto-derives from the title but can be
    # overridden manually. Useful for filtering/badging deals on the PTL calendar
    # (e.g. show "1g" or "3.5g" next to the deal card).

    # weight_value / weight_unit come from mint.weight.parsed.mixin; brand
    # calendar parses from the SKU/Category title (see _weight_source below) and
    # re-declares only its own help text.
    weight_value = fields.Float(
        help='Numeric weight/count parsed from the SKU/Category title (e.g. "1g AIO" → 1.0). '
             'Manually editable; clear to auto-recompute from the title.',
    )

    def _weight_source(self):
        return (self.sku_or_category, self.promo_text)

    @api.depends('sku_or_category', 'promo_text')
    def _compute_weight(self):
        # Source fields + deps are brand-calendar-specific; the parse/assign body
        # lives in mint.weight.parsed.mixin.
        return super()._compute_weight()

    # ── Vendor funding (per-entry override; defaults inherited from campaign) ──

    # vendor_funding_percent / currency_id come from mint.vendor.funding.mixin;
    # only the per-entry label is re-declared here.
    vendor_funding_amount = fields.Monetary(string='Vendor Funding (Per Entry)')

    # ── Scheduling ────────────────────────────────────────────────────────

    start_time = fields.Float(
        string='Start Time (hour)',
        default=0.0,
        help='Hour of day (24h float) when this entry becomes active. 0/0 = all day.',
    )
    end_time = fields.Float(
        string='End Time (hour)',
        default=0.0,
        help='Hour of day (24h float) when this entry stops being active. 0/0 = all day.',
    )

    # ── Linked PTL artifacts (existing) ──────────────────────────────────

    deal_id = fields.Many2one(
        'mint.ptl.deal',
        string='PTL Deal',
        help='The deal template linked to this calendar slot',
    )
    submission_id = fields.Many2one(
        'mint.deal.submission',
        string='Source Submission',
        help='The vendor submission this entry originated from',
    )
    ptl_day_id = fields.Many2one(
        'mint.ptl.day',
        string='PTL Day',
        help='The PTL day this entry was added to',
        readonly=True,
    )
    slot_type = fields.Selection(
        selection=[
            ('featured', 'Featured'),
            ('standard', 'Standard'),
            ('edlp', 'EDLP'),
        ],
        string='Slot Type',
        default='standard',
    )

    # ── State machine ────────────────────────────────────────────────────

    state = fields.Selection(
        selection=[
            ('tentative', 'Tentative'),
            ('confirmed', 'Confirmed'),
            ('approved', 'Approved'),
            ('published', 'Published'),
        ],
        string='Status',
        default='tentative',
        tracking=True,
    )
    is_published = fields.Boolean(
        string='Published',
        default=False,
        tracking=True,
        help='Marketing flips this when the entry is greenlit for the daily-deals carousel.',
    )

    notes = fields.Text(string='Notes')

    _sql_constraints = [
        ('brand_date_market_sku_uniq',
         'unique(brand_id, date, market_id, sku_or_category, campaign_id)',
         'Only one entry per brand × date × market × SKU/category × campaign.'),
    ]

    # ── State transitions ────────────────────────────────────────────────

    def action_confirm(self):
        self.filtered(lambda e: e.state == 'tentative').write({'state': 'confirmed'})

    def action_approve(self):
        self.filtered(lambda e: e.state in ('tentative', 'confirmed')).write({'state': 'approved'})

    def action_publish(self):
        """Marketing publish — flips is_published, advances state."""
        self.filtered(lambda e: e.state in ('confirmed', 'approved')).write({
            'state': 'published',
            'is_published': True,
        })

    def action_unpublish(self):
        self.filtered(lambda e: e.state == 'published').write({
            'state': 'approved',
            'is_published': False,
        })

    # ─── Plot Volume Guardrail ───────────────────────────────────────────
    #
    # Hard cap on (entries × days) inserted into mint_ptl_day_deal_rel by
    # action_add_to_ptl. Defaults: 200 normal, 1000 if any entry in the
    # batch is EDLP (intentional everyday-low-price volume). Configurable
    # via ir.config_parameter, bypassable via context['bypass_plot_guardrail'].

    GUARDRAIL_PARAM_DEFAULT = 'mint_command_center.plot_max_entries_default'
    GUARDRAIL_PARAM_HIGH = 'mint_command_center.plot_max_entries_high'
    GUARDRAIL_DEFAULT_VALUE = 200
    GUARDRAIL_HIGH_VALUE = 1000

    def _get_plot_guardrail_threshold(self, eligible):
        """Return the row-cap that applies to this eligible recordset."""
        get_param = self.env['ir.config_parameter'].sudo().get_param
        try:
            default_cap = int(get_param(
                self.GUARDRAIL_PARAM_DEFAULT, self.GUARDRAIL_DEFAULT_VALUE))
            high_cap = int(get_param(
                self.GUARDRAIL_PARAM_HIGH, self.GUARDRAIL_HIGH_VALUE))
        except (ValueError, TypeError):
            default_cap = self.GUARDRAIL_DEFAULT_VALUE
            high_cap = self.GUARDRAIL_HIGH_VALUE
        has_edlp = any(e.slot_type == 'edlp' for e in eligible)
        return high_cap if has_edlp else default_cap

    def _check_plot_volume_guardrail(self, eligible):
        """Abort plotting if eligible entry count > applicable threshold."""
        if self.env.context.get('bypass_plot_guardrail'):
            return
        if not eligible:
            return
        threshold = self._get_plot_guardrail_threshold(eligible)
        count = len(eligible)
        if count <= threshold:
            return
        kind = 'EDLP-aware' if any(e.slot_type == 'edlp' for e in eligible) else 'default'
        raise UserError(
            f"Plot Volume Guardrail: refusing to plot {count} entries in one "
            f"action — exceeds the {kind} threshold of {threshold}.\n\n"
            f"This guardrail exists to catch fan-out bugs before they pollute "
            f"the PTL (the 3,407-entry WTF-over-expansion class of incident).\n\n"
            f"If you genuinely intended this volume, raise the threshold via "
            f"Settings → Technical → Parameters "
            f"({self.GUARDRAIL_PARAM_DEFAULT} or {self.GUARDRAIL_PARAM_HIGH}), "
            f"or split the operation into smaller batches."
        )

    # ─── Conflict detection (F3a) ────────────────────────────────────────

    def _detect_plot_conflicts(self, eligible):
        """For each eligible entry that targets an existing PTL day, find
        existing deals on that day with overlapping (brand_id, product_category).

        Returns a list of dicts:
            [{'entry_id': int, 'day_id': int, 'existing_deal_ids': [int]}]
        """
        Day = self.env['mint.ptl.day']
        out = []
        for entry in eligible:
            day = Day.search([
                ('date', '=', entry.date),
                ('market_id', '=', entry.market_id.id),
            ], limit=1)
            if not day or not day.deal_ids:
                continue
            ent_deal = entry.deal_id
            existing = day.deal_ids.filtered(
                lambda d, ent=ent_deal: (
                    d.id != ent.id
                    and d.brand_id.id == ent.brand_id.id
                    and (d.product_category or '') == (ent.product_category or '')
                )
            )
            if existing:
                out.append({
                    'entry_id': entry.id,
                    'day_id': day.id,
                    'existing_deal_ids': existing.ids,
                })
        return out

    def _open_conflict_resolution_wizard(self, conflicts, eligible):
        """Return an action that opens the Conflict Resolution wizard pre-
        filled with the detected overlap lines."""
        Wizard = self.env['mint.ptl.conflict.resolution.wizard']
        line_vals = []
        for c in conflicts:
            # One wizard line per (entry × existing_deal) pair; usually one
            # existing deal per overlap but we tolerate multiples.
            for existing_id in c['existing_deal_ids']:
                line_vals.append((0, 0, {
                    'entry_id': c['entry_id'],
                    'day_id': c['day_id'],
                    'existing_deal_id': existing_id,
                    'resolution': 'replace',
                }))
        conflict_entry_ids = {c['entry_id'] for c in conflicts}
        non_conflict_count = len(eligible) - len(conflict_entry_ids)
        wiz = Wizard.create({
            'entry_ids': [(6, 0, eligible.ids)],
            'conflict_line_ids': line_vals,
            'non_conflict_count': non_conflict_count,
        })
        return {
            'name': 'Resolve Plot Conflicts',
            'type': 'ir.actions.act_window',
            'res_model': Wizard._name,
            'res_id': wiz.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_add_to_ptl(self):
        """Create or link to a PTL Day and attach the deal.

        Flow:
          1. F2 plot-volume guardrail (abort if batch too large)
          2. F3a conflict detection (open wizard if overlaps found)
          3. Plot normally otherwise
        """
        eligible = self.filtered(
            lambda e: e.deal_id and e.state in ('tentative', 'confirmed', 'approved')
        )
        self._check_plot_volume_guardrail(eligible)

        if not self.env.context.get('bypass_conflict_check'):
            conflicts = self._detect_plot_conflicts(eligible)
            if conflicts:
                return self._open_conflict_resolution_wizard(conflicts, eligible)

        PtlDay = self.env['mint.ptl.day']
        for entry in eligible:
            day = PtlDay.search([
                ('date', '=', entry.date),
                ('market_id', '=', entry.market_id.id),
            ], limit=1)
            if not day:
                day = PtlDay.create({
                    'date': entry.date,
                    'market_id': entry.market_id.id,
                })
            day.write({'deal_ids': [(4, entry.deal_id.id)]})
            entry.write({
                'ptl_day_id': day.id,
                'state': 'confirmed' if entry.state == 'tentative' else entry.state,
            })
