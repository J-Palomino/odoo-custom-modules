from odoo import api, models

# (key, tag xmlid, heading, one-line explanation, who it belongs to)
# "assigned": the viewer is an assignee. "following": the viewer follows it.
BOARD_SECTIONS = (
    ('decision', 'mint_my_board.tag_decision', 'Decision needed',
     'We need your call before work can continue.', 'assigned'),
    ('needs_you', 'mint_my_board.tag_needs_you', 'Needs you',
     'Only you can do these.', 'assigned'),
    ('request', 'mint_my_board.tag_request', 'Your requests',
     'Things you asked for, and where they stand.', 'following'),
)

CLOSED_STATES = ('1_done', '1_canceled')


class ProjectTask(models.Model):
    _inherit = 'project.task'

    @api.model
    def _my_board_sections(self, user=None):
        """The /my/board sections for ``user`` (default: the current user).

        Searches with the user's own access rights, but across all of their
        companies: the website request is pinned to the website's company, and
        the backend picker can leave the parent company unticked, either of
        which would otherwise hide every task on the board.
        """
        user = user or self.env.user
        Task = self.with_user(user).with_context(
            allowed_company_ids=user.company_ids.ids,
        )
        sections = []
        for key, xmlid, title, blurb, scope in BOARD_SECTIONS:
            tag = self.env.ref(xmlid, raise_if_not_found=False)
            if not tag:
                continue
            if scope == 'assigned':
                who = ('user_ids', 'in', user.ids)
            else:
                who = ('message_partner_ids', 'in', user.partner_id.ids)
            tasks = Task.search(
                [('tag_ids', 'in', tag.ids),
                 ('state', 'not in', CLOSED_STATES),
                 who],
                order='priority desc, write_date desc',
            )
            sections.append({
                'key': key,
                'title': title,
                'blurb': blurb,
                'tasks': tasks,
            })
        return sections
