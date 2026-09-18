from odoo.exceptions import AccessError
from odoo.tests import new_test_user, tagged
from odoo.tests.common import TransactionCase


@tagged('post_install', '-at_install')
class TestLinkTrackerEmployeeAccess(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.employee = new_test_user(cls.env, login='lt_employee', groups='base.group_user')
        cls.colleague = new_test_user(cls.env, login='lt_colleague', groups='base.group_user')
        cls.marketing = new_test_user(
            cls.env, login='lt_marketing',
            groups='base.group_user,mass_mailing.group_mass_mailing_user',
        )
        cls.Tracker = cls.env['link.tracker']

    def _create_as(self, user, suffix):
        # A title avoids link_tracker fetching the page to derive one.
        return self.Tracker.with_user(user).create({
            'url': f'https://example.com/{suffix}',
            'title': suffix,
        })

    def test_employee_can_create_link_with_short_code(self):
        link = self._create_as(self.employee, 'create')
        self.assertTrue(link.code)
        self.assertTrue(link.short_url)

    def test_employee_can_edit_own_link(self):
        link = self._create_as(self.employee, 'own')
        link.write({'title': 'renamed'})
        self.assertEqual(link.title, 'renamed')

    def test_employee_cannot_repoint_colleague_link(self):
        link = self._create_as(self.colleague, 'theirs')
        with self.assertRaises(AccessError):
            link.with_user(self.employee).write({'url': 'https://example.com/hijack'})

    def test_employee_can_read_colleague_link(self):
        link = self._create_as(self.colleague, 'readable')
        self.assertEqual(link.with_user(self.employee).url, 'https://example.com/readable')

    def test_employee_cannot_delete_own_link(self):
        link = self._create_as(self.employee, 'keep')
        with self.assertRaises(AccessError):
            link.unlink()

    def test_marketing_can_edit_any_link(self):
        link = self._create_as(self.employee, 'shared')
        link.with_user(self.marketing).write({'title': 'edited by marketing'})
        self.assertEqual(link.title, 'edited by marketing')

    def test_link_tracker_menu_visible_to_employees(self):
        visible = self.env['ir.ui.menu'].with_user(self.employee)._visible_menu_ids()
        self.assertIn(self.env.ref('utm.menu_link_tracker_root').id, visible)
        self.assertIn(self.env.ref('link_tracker.link_tracker_menu_main').id, visible)
