# -*- coding: utf-8 -*-
"""GET /mint/sample-review/<line_id>/<token> — open the review survey for one line.

The storefront's /orders renders this link on every employee-sample line (the
token is an HMAC of the line id, so only the line's owner ever receives it).
Opening it binds a survey response to the line and redirects into the survey,
which is how "has this sample been reviewed?" becomes an ID lookup rather than
a guess from the free-text product the employee types.

Why the response is created HERE and not by the orders API: the survey rejects
a pre-made response whose partner differs from the logged-in viewer
(``answer_wrong_user``), and many employees are signed in to letsgomint.us as
internal users. Only this request knows who is actually viewing.

Any URL fragment on the link (the storefront's prefill: name, email, store,
product, brand_id) survives the redirect, so the prefill script keeps working.
"""
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class MintSampleReview(http.Controller):

    @http.route('/mint/sample-review/<int:line_id>/<string:token>',
                type='http', auth='public', methods=['GET'], sitemap=False)
    def sample_review(self, line_id, token, **kw):
        Line = request.env['mint.pos.order.line'].sudo()
        line = Line.browse(line_id).exists()
        if not line or not line._check_sample_review_token(token) or not line._is_sample_line():
            return request.not_found()

        survey = Line._sample_review_survey()
        if not survey or not survey.active:
            _logger.warning('sample-review: survey not configured/active (line %s)', line.id)
            return request.not_found()

        viewer = request.env.user
        partner = viewer.partner_id if not viewer._is_public() else request.env['res.partner']

        UserInput = request.env['survey.user_input'].sudo()
        answer = UserInput.search([
            ('survey_id', '=', survey.id),
            ('mint_order_line_id', '=', line.id),
            ('partner_id', '=', partner.id),
            ('state', '!=', 'done'),
            ('test_entry', '=', False),
        ], order='id desc', limit=1)
        if not answer:
            answer = survey._create_answer(
                user=viewer if partner else False,
                check_attempts=False,
                mint_order_line_id=line.id,
            )
        return request.redirect(answer.get_start_url(), local=True)
