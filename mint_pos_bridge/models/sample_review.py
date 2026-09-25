# -*- coding: utf-8 -*-
"""Employee sample reviews, tied to the POS line they review.

Employees get sample SKUs rung as penny comps and are asked to review each one
in the "Employee Sample Product Review" survey (194 on prod). The survey only
records email, brand and a free-text product, so nothing could say WHICH line a
response reviews — and /orders could not tell an employee what they still owe.

The link is by ID: a response carries ``mint_order_line_id``, set when the
employee opens the survey from that line's signed link (see
controllers/sample_review.py). A line counts as reviewed once any completed
response points at it. Nothing here is stored on mint.pos.order.line — with
~4.8M lines a stored compute would have to backfill every row on upgrade, and
the per-caller sets these helpers work on are small.
"""
import re

from odoo import api, fields, models
from odoo.tools.misc import consteq, hmac as odoo_hmac

# Same rule the storefront badge uses (orders.astro isSampleItem) so the
# "to review" list and the SAMPLE chip never disagree about what a sample is.
SAMPLE_TOKEN_RE = re.compile(r'\bsamples?\b', re.IGNORECASE)

SAMPLE_REVIEW_SURVEY_PARAM = 'mint_pos_bridge.sample_review_survey_id'
SAMPLE_REVIEW_HMAC_SCOPE = 'mint_pos_bridge.sample_review'


class SurveyUserInput(models.Model):
    _inherit = 'survey.user_input'

    mint_order_line_id = fields.Many2one(
        'mint.pos.order.line', string='Reviewed POS Line',
        index='btree_not_null', ondelete='set null', copy=False,
        help='The employee-sample order line this response reviews.')


class MintPosOrderLine(models.Model):
    _inherit = 'mint.pos.order.line'

    review_input_ids = fields.One2many(
        'survey.user_input', 'mint_order_line_id', string='Sample Reviews')

    def _is_sample_line(self):
        self.ensure_one()
        return bool(SAMPLE_TOKEN_RE.search(self.product_name or '')
                    or SAMPLE_TOKEN_RE.search(self.category or ''))

    def _sample_review_token(self):
        self.ensure_one()
        return odoo_hmac(self.env(su=True), SAMPLE_REVIEW_HMAC_SCOPE, str(self.id))

    def _check_sample_review_token(self, token):
        self.ensure_one()
        return bool(token) and consteq(str(token), self._sample_review_token())

    def _sample_review_path(self):
        self.ensure_one()
        return '/mint/sample-review/%d/%s' % (self.id, self._sample_review_token())

    def _sample_reviewed_ids(self):
        """Ids of the lines in ``self`` with at least one completed review."""
        if not self:
            return set()
        done = self.env['survey.user_input'].sudo().search([
            ('mint_order_line_id', 'in', self.ids),
            ('state', '=', 'done'),
        ])
        return set(done.mint_order_line_id.ids)

    @api.model
    def _sample_review_survey(self):
        """The review survey, or an empty recordset if it is not configured."""
        survey_id = self.env['ir.config_parameter'].sudo().get_param(
            SAMPLE_REVIEW_SURVEY_PARAM, '0')
        try:
            survey_id = int(survey_id)
        except (TypeError, ValueError):
            survey_id = 0
        return self.env['survey.survey'].sudo().browse(survey_id).exists()


class MintPosOrder(models.Model):
    _inherit = 'mint.pos.order'

    @api.model
    def _pending_sample_review_orders(self, domain, scan_limit=500):
        """Orders within ``domain`` holding a sample line nobody has reviewed.

        ``domain`` must already be scoped to one caller — this is the
        "samples you still owe a review" list, not a report. Newest first.
        """
        orders = self.sudo().search(domain, order='placed_at desc', limit=scan_limit)
        if not orders:
            return orders
        candidates = self.env['mint.pos.order.line'].sudo().search([
            ('order_id', 'in', orders.ids),
            '|', ('product_name', 'ilike', 'sample'), ('category', 'ilike', 'sample'),
        ])
        samples = candidates.filtered(lambda l: l._is_sample_line())
        reviewed = samples._sample_reviewed_ids()
        pending = samples.filtered(lambda l: l.id not in reviewed).order_id
        return orders.filtered(lambda o: o in pending)
