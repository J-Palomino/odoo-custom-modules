import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class DecisionBoardController(http.Controller):

    @http.route('/decisions', type='http', auth='user', website=True)
    def decisions_index(self, **kw):
        user = request.env.user
        decisions = request.env['mint.decision'].sudo().search(
            [('state', 'in', ('open', 'closed'))], order='sequence, id'
        ).filtered(lambda d: d._user_can_view(user))
        return request.render('mint_decision_board.decision_index', {
            'user': user,
            'decisions': decisions,
        })

    @http.route('/tickets/decisions', type='http', auth='user', website=True)
    def tickets_decisions(self, **kw):
        """C-Suite alternate view, surfaced from the /tickets page."""
        Decision = request.env['mint.decision']
        if not Decision._is_csuite(request.env.user):
            return request.redirect('/tickets')
        decisions = Decision.sudo().search(
            [('state', 'in', ('open', 'closed'))], order='sequence, id')
        return request.render('mint_decision_board.decisions_tickets_list', {
            'user': request.env.user,
            'decisions': decisions,
        })

    @http.route('/decision/<string:slug>', type='http', auth='user',
                website=True, methods=['GET', 'POST'])
    def decision_page(self, slug, **post):
        decision = request.env['mint.decision'].sudo().search(
            [('slug', '=', slug)], limit=1)
        if not decision:
            return request.not_found()

        is_admin = request.env.user.has_group('base.group_system')

        # Drafts are visible only to settings admins (for preview).
        if decision.state == 'draft' and not is_admin:
            return request.not_found()

        # Per-board audience gate (C-Suite-only boards).
        if not is_admin and not decision._user_can_view(request.env.user):
            return request.not_found()

        if request.httprequest.method == 'POST':
            return self._handle_vote(decision, post)
        return request.render('mint_decision_board.decision_page',
                              self._context(decision))

    # ------------------------------------------------------------------

    def _handle_vote(self, decision, post):
        if decision.state != 'open':
            ctx = self._context(decision)
            ctx['error'] = 'Voting is closed for this decision.'
            return request.render('mint_decision_board.decision_page', ctx)

        try:
            option_id = int(post.get('option_id') or 0)
        except (ValueError, TypeError):
            option_id = 0
        option = decision.option_ids.filtered(lambda o: o.id == option_id)
        if not option:
            ctx = self._context(decision)
            ctx['error'] = 'Please pick an option before submitting.'
            return request.render('mint_decision_board.decision_page', ctx)

        vals = {
            'option_id': option.id,
            'comment': (post.get('comment') or '').strip(),
        }
        Vote = request.env['mint.decision.vote'].sudo()
        existing = Vote.search([
            ('decision_id', '=', decision.id),
            ('voter_id', '=', request.env.user.id),
        ], limit=1)
        try:
            if existing:
                existing.write(vals)
            else:
                vals.update({
                    'decision_id': decision.id,
                    'voter_id': request.env.user.id,
                })
                Vote.create(vals)
        except Exception as e:  # pragma: no cover - defensive
            _logger.error('Decision vote save failed: %s', e)
            ctx = self._context(decision)
            ctx['error'] = 'Could not save your vote. Please try again.'
            return request.render('mint_decision_board.decision_page', ctx)

        ctx = self._context(decision)
        ctx['success'] = 'Your decision is recorded. You can update it any time.'
        return request.render('mint_decision_board.decision_page', ctx)

    def _context(self, decision):
        Vote = request.env['mint.decision.vote'].sudo()
        my_vote = Vote.search([
            ('decision_id', '=', decision.id),
            ('voter_id', '=', request.env.user.id),
        ], limit=1)
        votes = decision.vote_ids.sorted(lambda v: v.write_date, reverse=True)
        return {
            'user': request.env.user,
            'decision': decision,
            'options': decision.option_ids.sorted(lambda o: (o.sequence, o.id)),
            'my_vote': my_vote,
            'votes': votes,
            'tally': decision.get_tally(),
            'total_votes': len(votes),
            'can_vote': decision.state == 'open',
            'error': None,
            'success': None,
        }
