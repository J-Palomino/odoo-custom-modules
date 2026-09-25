# -*- coding: utf-8 -*-
"""Employee sample reviews are tied to the POS line they review, by ID.

Traces to 2026-09-25: employees' sample orders fell off /orders (it shows the
20 newest) and nothing could say which samples had been reviewed — survey 194
only stores email, brand and a free-text product. /orders now lists samples
still owing a review, and the survey response carries the line it reviews.
"""
from odoo.tests.common import HttpCase, TransactionCase, tagged


class _SampleReviewFixture:

    def _setup_fixture(self):
        self.Order = self.env['mint.pos.order'].sudo()
        self.Line = self.env['mint.pos.order.line'].sudo()
        self.survey = self.env['survey.survey'].sudo().create({
            'title': 'Employee Sample Product Review (test)',
            'access_mode': 'public',
            'users_login_required': False,
            'question_ids': [(0, 0, {
                'title': 'What product did you sample?',
                'question_type': 'text_box',
            })],
        })
        self.env['ir.config_parameter'].sudo().set_param(
            'mint_pos_bridge.sample_review_survey_id', str(self.survey.id))
        self.partner = self.env['res.partner'].sudo().create({
            'name': 'Sample Tester', 'email': 'sample.tester@example.com'})

    def _order(self, *names, placed_at='2026-09-01 12:00:00', partner=None):
        return self.Order.create({
            'company_id': self.env.company.id,
            'partner_id': (partner or self.partner).id,
            'state': 'completed',
            'placed_at': placed_at,
            'line_ids': [(0, 0, {'product_name': n}) for n in names],
        })

    def _review(self, line, state='done'):
        return self.env['survey.user_input'].sudo().create({
            'survey_id': self.survey.id,
            'mint_order_line_id': line.id,
            'state': state,
        })


@tagged('post_install', '-at_install')
class TestSampleReviewModel(_SampleReviewFixture, TransactionCase):

    def setUp(self):
        super().setUp()
        self._setup_fixture()

    def test_sample_detection_matches_the_storefront_badge(self):
        order = self._order(
            'Journeyman - Sample Employee - Beverage (100mg)',
            'Brand - Flower - Samples (E)',
            'Brand - Sampler Pack (7g)',
            'Brand - Vape (1g)',
        )
        flags = [l._is_sample_line() for l in order.line_ids]
        self.assertEqual(flags, [True, True, False, False],
                         '"Sampler" is a product, not a sample — same rule as isSampleItem')

    def test_only_a_completed_response_counts_as_reviewed(self):
        order = self._order('A - Sample Employee - X', 'B - Sample Employee - Y')
        a, b = order.line_ids
        self._review(a, 'done')
        self._review(b, 'in_progress')
        self.assertEqual(order.line_ids._sample_reviewed_ids(), {a.id})

    def test_token_is_bound_to_the_line(self):
        order = self._order('A - Sample Employee - X', 'B - Sample Employee - Y')
        a, b = order.line_ids
        self.assertTrue(a._check_sample_review_token(a._sample_review_token()))
        self.assertFalse(a._check_sample_review_token(b._sample_review_token()))
        self.assertFalse(a._check_sample_review_token(''))

    def test_pending_lists_orders_with_an_unreviewed_sample_newest_first(self):
        done = self._order('A - Sample Employee - X', placed_at='2026-09-10 10:00:00')
        self._review(done.line_ids)
        older = self._order('B - Sample Employee - Y', placed_at='2026-08-01 10:00:00')
        newer = self._order('C - Sample Employee - Z', 'Regular Vape (1g)',
                            placed_at='2026-09-20 10:00:00')
        self._order('Regular Flower (3.5g)', placed_at='2026-09-21 10:00:00')
        stranger = self.env['res.partner'].sudo().create({'name': 'Someone Else'})
        self._order('D - Sample Employee - W', partner=stranger)

        pending = self.Order._pending_sample_review_orders(
            [('partner_id', '=', self.partner.id)])
        self.assertEqual(pending, newer | older)
        self.assertEqual(pending[0], newer)

    def test_partly_reviewed_order_stays_pending(self):
        order = self._order('A - Sample Employee - X', 'B - Sample Employee - Y')
        self._review(order.line_ids[0])
        pending = self.Order._pending_sample_review_orders(
            [('partner_id', '=', self.partner.id)])
        self.assertEqual(pending, order)

    def test_unconfigured_survey_disables_reviews(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'mint_pos_bridge.sample_review_survey_id', '0')
        self.assertFalse(self.Line._sample_review_survey())


@tagged('post_install', '-at_install')
class TestSampleReviewRoute(_SampleReviewFixture, HttpCase):

    def setUp(self):
        super().setUp()
        self._setup_fixture()
        self.order = self._order('A - Sample Employee - X', 'Regular Vape (1g)')
        self.sample, self.regular = self.order.line_ids

    def _get(self, path):
        return self.url_open(path, allow_redirects=False)

    def test_valid_link_binds_a_response_and_redirects_into_the_survey(self):
        res = self._get(self.sample._sample_review_path())
        self.assertIn(res.status_code, (301, 302, 303))
        answer = self.env['survey.user_input'].sudo().search(
            [('mint_order_line_id', '=', self.sample.id)])
        self.assertEqual(len(answer), 1)
        self.assertFalse(answer.partner_id, 'public viewer → no partner on the response')
        self.assertIn('/survey/start/%s' % self.survey.access_token, res.headers['Location'])
        self.assertIn('answer_token=%s' % answer.access_token, res.headers['Location'])

    def test_reopening_reuses_the_open_response(self):
        self._get(self.sample._sample_review_path())
        self._get(self.sample._sample_review_path())
        self.assertEqual(self.env['survey.user_input'].sudo().search_count(
            [('mint_order_line_id', '=', self.sample.id)]), 1)

    def test_logged_in_viewer_gets_a_response_under_their_own_partner(self):
        """Otherwise the survey rejects it as answer_wrong_user."""
        self.authenticate('admin', 'admin')
        self._get(self.sample._sample_review_path())
        answer = self.env['survey.user_input'].sudo().search(
            [('mint_order_line_id', '=', self.sample.id)])
        self.assertEqual(answer.partner_id, self.env.ref('base.user_admin').partner_id)

    def test_bad_token_or_non_sample_line_is_not_found(self):
        forged = '/mint/sample-review/%d/%s' % (
            self.sample.id, self.regular._sample_review_token())
        self.assertEqual(self._get(forged).status_code, 404)
        self.assertEqual(self._get(self.regular._sample_review_path()).status_code, 404)
        self.assertFalse(self.env['survey.user_input'].sudo().search(
            [('mint_order_line_id', 'in', self.order.line_ids.ids)]))
