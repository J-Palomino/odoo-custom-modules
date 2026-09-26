from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestMyBoard(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = cls.env['res.users'].create({
            'name': 'Board Tester',
            'login': 'board.tester@example.com',
            'group_ids': [(6, 0, [cls.env.ref('project.group_project_user').id])],
        })
        cls.other = cls.env['res.users'].create({
            'name': 'Someone Else',
            'login': 'someone.else@example.com',
            'group_ids': [(6, 0, [cls.env.ref('project.group_project_user').id])],
        })
        cls.project = cls.env['project.project'].create({
            'name': 'Board Test Project',
            'privacy_visibility': 'employees',
        })
        cls.tag_decision = cls.env.ref('mint_my_board.tag_decision')
        cls.tag_needs_you = cls.env.ref('mint_my_board.tag_needs_you')
        cls.tag_request = cls.env.ref('mint_my_board.tag_request')

    def _task(self, name, tag, users=None, **vals):
        return self.env['project.task'].create({
            'name': name,
            'project_id': self.project.id,
            'tag_ids': [(6, 0, tag.ids)],
            'user_ids': [(6, 0, (users or self.env['res.users']).ids)],
            **vals,
        })

    def _board(self, user):
        return {s['key']: s['tasks'] for s in
                self.env['project.task']._my_board_sections(user)}

    def test_sections_follow_tag_and_scope(self):
        decision = self._task('Pick the URL shape', self.tag_decision, self.user)
        chore = self._task('Add the service account', self.tag_needs_you, self.user)
        request = self._task('Sitemap is empty', self.tag_request, self.other)
        request.message_subscribe(partner_ids=self.user.partner_id.ids)

        board = self._board(self.user)
        self.assertEqual(list(board), ['decision', 'needs_you', 'request'])
        self.assertEqual(board['decision'], decision)
        self.assertEqual(board['needs_you'], chore)
        self.assertEqual(board['request'], request)

    def test_other_peoples_items_stay_off_the_board(self):
        self._task('Not mine', self.tag_needs_you, self.other)
        self._task('Not followed', self.tag_request, self.other)
        board = self._board(self.user)
        self.assertFalse(board['needs_you'])
        self.assertFalse(board['request'])

    def test_closed_tasks_drop_off(self):
        self._task('Done already', self.tag_needs_you, self.user, state='1_done')
        self._task('Cancelled', self.tag_decision, self.user, state='1_canceled')
        board = self._board(self.user)
        self.assertFalse(board['needs_you'])
        self.assertFalse(board['decision'])

    def test_subtasks_are_included(self):
        parent = self._task('Parent', self.tag_request, self.other)
        child = self._task('AC04: decide canonicals', self.tag_decision,
                           self.user, parent_id=parent.id)
        self.assertEqual(self._board(self.user)['decision'], child)

    def test_untagged_tasks_are_ignored(self):
        self.env['project.task'].create({
            'name': 'No tag', 'project_id': self.project.id,
            'user_ids': [(6, 0, self.user.ids)],
        })
        self.assertFalse(any(self._board(self.user).values()))
