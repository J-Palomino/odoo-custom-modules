from odoo import http
from odoo.http import request


class MyBoardController(http.Controller):

    @http.route('/my/board', type='http', auth='user', website=True,
                sitemap=False)
    def my_board(self, **kw):
        sections = request.env['project.task']._my_board_sections()
        return request.render('mint_my_board.my_board', {
            'user': request.env.user,
            'sections': sections,
            'total': sum(len(s['tasks']) for s in sections),
        })
