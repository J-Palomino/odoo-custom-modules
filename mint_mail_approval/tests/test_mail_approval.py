# -*- coding: utf-8 -*-
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestMailApprovalGate(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Mail = cls.env['mail.mail']
        cls.Rule = cls.env['mint.mail.approval.rule']
        cls.param = cls.env['ir.config_parameter'].sudo()
        # Start from a known rule set rather than the shipped seed data.
        cls.Rule.search([]).unlink()

    def _mail(self, **vals):
        base = {
            'subject': 'Test',
            'email_to': 'someone@letsgomint.com',
            'state': 'outgoing',
            'body_html': '<p>hi</p>',
        }
        base.update(vals)
        return self.Mail.create(base)

    def _send(self, mails):
        """Call send() with the real SMTP layer stubbed out."""
        with patch('odoo.addons.mail.models.mail_mail.MailMail._send') as sender:
            mails.send()
            return sender

    # ------------------------------------------------------------------
    def test_no_rules_holds_everything(self):
        """Fail-closed: with no rules, nothing sends."""
        mail = self._mail()
        sender = self._send(mail)
        sender.assert_not_called()
        self.assertEqual(mail.approval_state, 'pending')

    def test_auto_rule_sends(self):
        self.Rule.create({
            'name': 'reset', 'sequence': 1, 'action': 'auto',
            'match_subject': '^Security Update',
        })
        mail = self._mail(subject='Security Update: Password Changed')
        sender = self._send(mail)
        sender.assert_called()
        self.assertEqual(mail.approval_state, 'auto')

    def test_hold_rule_blocks(self):
        self.Rule.create({
            'name': 'welcome', 'sequence': 1, 'action': 'hold',
            'match_subject': 'Welcome to Mint',
        })
        mail = self._mail(subject='Welcome to Mint Cannabis!')
        sender = self._send(mail)
        sender.assert_not_called()
        self.assertEqual(mail.approval_state, 'pending')

    def test_first_match_wins(self):
        self.Rule.create({
            'name': 'specific', 'sequence': 1, 'action': 'auto',
            'match_subject': 'Welcome to Mint',
        })
        self.Rule.create({'name': 'catch all', 'sequence': 99, 'action': 'hold'})
        mail = self._mail(subject='Welcome to Mint Cannabis!')
        self._send(mail)
        self.assertEqual(mail.approval_state, 'auto')

    def test_model_criterion(self):
        self.Rule.create({
            'name': 'users only', 'sequence': 1, 'action': 'auto',
            'match_model': 'res.users',
        })
        self.Rule.create({'name': 'catch all', 'sequence': 99, 'action': 'hold'})
        # res.partner rather than e.g. maintenance.request: the criterion only
        # needs a non-matching model, and it must exist in a minimal registry
        # (core mail resolves env[model] while building the message).
        matching = self._mail(model='res.users', res_id=self.env.uid)
        other = self._mail(model='res.partner', res_id=self.env.user.partner_id.id)
        self._send(matching | other)
        self.assertEqual(matching.approval_state, 'auto')
        self.assertEqual(other.approval_state, 'pending')

    def test_recipient_domain_criterion(self):
        self.Rule.create({
            'name': 'internal', 'sequence': 1, 'action': 'auto',
            'match_recipient_domains': 'letsgomint.com, brightroot.com',
        })
        self.Rule.create({'name': 'catch all', 'sequence': 99, 'action': 'hold'})
        internal = self._mail(email_to='a@brightroot.com')
        external = self._mail(email_to='b@gmail.com')
        self._send(internal | external)
        self.assertEqual(internal.approval_state, 'auto')
        self.assertEqual(external.approval_state, 'pending')

    def test_mixed_batch_only_sends_approved(self):
        """A held mail must not drag an approved one down, or vice versa."""
        self.Rule.create({
            'name': 'ok', 'sequence': 1, 'action': 'auto',
            'match_subject': '^OK',
        })
        self.Rule.create({'name': 'catch all', 'sequence': 99, 'action': 'hold'})
        good = self._mail(subject='OK to send')
        bad = self._mail(subject='Needs review')
        with patch('odoo.addons.mail.models.mail_mail.MailMail._send') as sender:
            (good | bad).send()
            sent_ids = set()
            for call in sender.call_args_list:
                sent_ids.update(call.args[0].ids if call.args else [])
        self.assertEqual(good.approval_state, 'auto')
        self.assertEqual(bad.approval_state, 'pending')

    def test_approve_then_sends(self):
        self.Rule.create({'name': 'catch all', 'sequence': 99, 'action': 'hold'})
        mail = self._mail()
        self._send(mail)
        self.assertEqual(mail.approval_state, 'pending')

        self.env.user.group_ids = [(4, self.env.ref('mint_mail_approval.group_mail_approver').id)]
        with patch('odoo.addons.mail.models.mail_mail.MailMail._send') as sender:
            mail.action_mma_approve()
            sender.assert_called()
        self.assertEqual(mail.approval_state, 'approved')
        self.assertEqual(mail.approval_uid, self.env.user)

    def test_approve_requeues_failed_mail(self):
        """A mail that already failed must go back to 'outgoing' on approval."""
        self.Rule.create({'name': 'catch all', 'sequence': 99, 'action': 'hold'})
        mail = self._mail(state='exception', failure_reason='111\nConnection refused')
        self.env.user.group_ids = [(4, self.env.ref('mint_mail_approval.group_mail_approver').id)]
        with patch('odoo.addons.mail.models.mail_mail.MailMail._send'):
            mail.action_mma_approve()
        self.assertFalse(mail.failure_reason)

    def test_reject_cancels(self):
        self.Rule.create({'name': 'catch all', 'sequence': 99, 'action': 'hold'})
        mail = self._mail()
        self.env.user.group_ids = [(4, self.env.ref('mint_mail_approval.group_mail_approver').id)]
        mail.action_mma_reject()
        self.assertEqual(mail.approval_state, 'rejected')
        self.assertEqual(mail.state, 'cancel')
        sender = self._send(mail)
        sender.assert_not_called()

    def test_disabled_gate_is_transparent(self):
        self.param.set_param('mint_mail_approval.enabled', '0')
        self.addCleanup(self.param.set_param, 'mint_mail_approval.enabled', '1')
        mail = self._mail()
        sender = self._send(mail)
        sender.assert_called()

    def test_default_action_auto(self):
        self.param.set_param('mint_mail_approval.default_action', 'auto')
        self.addCleanup(self.param.set_param, 'mint_mail_approval.default_action', 'hold')
        mail = self._mail()
        sender = self._send(mail)
        sender.assert_called()
        self.assertEqual(mail.approval_state, 'auto')

    def test_unknown_default_action_fails_closed(self):
        self.param.set_param('mint_mail_approval.default_action', 'banana')
        self.addCleanup(self.param.set_param, 'mint_mail_approval.default_action', 'hold')
        mail = self._mail()
        sender = self._send(mail)
        sender.assert_not_called()

    def test_invalid_regex_rejected_at_write(self):
        from odoo.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            self.Rule.create({
                'name': 'bad', 'action': 'auto', 'match_subject': '[unclosed',
            })

    def test_expire_cron_cancels_stale(self):
        from odoo import fields
        self.Rule.create({'name': 'catch all', 'sequence': 99, 'action': 'hold'})
        mail = self._mail()
        self._send(mail)
        old = fields.Datetime.subtract(fields.Datetime.now(), days=30)
        self.env.cr.execute(
            "UPDATE mail_mail SET create_date = %s WHERE id = %s", (old, mail.id)
        )
        mail.invalidate_recordset(['create_date'])
        self.Mail._mma_cron_expire_pending()
        self.assertEqual(mail.approval_state, 'rejected')
        self.assertEqual(mail.state, 'cancel')
