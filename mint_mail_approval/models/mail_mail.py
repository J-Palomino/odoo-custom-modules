# -*- coding: utf-8 -*-
import email.utils
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

PARAM_ENABLED = 'mint_mail_approval.enabled'
PARAM_DEFAULT_ACTION = 'mint_mail_approval.default_action'
PARAM_EXPIRE_DAYS = 'mint_mail_approval.expire_days'


class MailMail(models.Model):
    _inherit = 'mail.mail'

    approval_state = fields.Selection(
        [('pending', 'Pending Approval'),
         ('approved', 'Approved'),
         ('auto', 'Auto-approved'),
         ('rejected', 'Rejected')],
        string='Approval',
        default='pending',
        index=True,
        copy=False,
        readonly=True,
    )
    approval_uid = fields.Many2one(
        'res.users', string='Approved By', readonly=True, copy=False,
    )
    approval_date = fields.Datetime(string='Approved On', readonly=True, copy=False)
    approval_note = fields.Char(string='Approval Note', copy=False)
    approval_rule_id = fields.Many2one(
        'mint.mail.approval.rule', string='Matched Rule', readonly=True, copy=False,
        ondelete='set null',
    )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @api.model
    def _mma_enabled(self):
        return self.env['ir.config_parameter'].sudo().get_param(PARAM_ENABLED, '1') == '1'

    @api.model
    def _mma_default_action(self):
        action = (self.env['ir.config_parameter'].sudo()
                  .get_param(PARAM_DEFAULT_ACTION, 'hold') or 'hold').strip().lower()
        # Anything unrecognised must fail closed.
        return 'auto' if action == 'auto' else 'hold'

    def _mma_recipient_domains(self):
        """Every recipient domain on this mail, lower-cased."""
        self.ensure_one()
        addrs = []
        for pattern in (self.email_to, self.email_cc):
            if pattern:
                addrs.extend(addr for _name, addr in email.utils.getaddresses([pattern]))
        addrs.extend(p.email for p in self.recipient_ids if p.email)
        return {
            a.rsplit('@', 1)[-1].strip().lower()
            for a in addrs
            if a and '@' in a
        }

    def _mma_apply_rules(self):
        """Classify any still-pending, never-classified mail against the rules."""
        unclassified = self.filtered(
            lambda m: m.approval_state == 'pending' and not m.approval_rule_id
        )
        if not unclassified:
            return

        rules = self.env['mint.mail.approval.rule'].sudo().search([])
        default_action = self._mma_default_action()
        matched_counts = {}

        for mail in unclassified:
            rule = rules._first_match(mail, mail._mma_recipient_domains())
            action = rule.action if rule else default_action
            vals = {'approval_rule_id': rule.id or False}
            if action == 'auto':
                vals.update({
                    'approval_state': 'auto',
                    'approval_date': fields.Datetime.now(),
                    'approval_note': _('Auto-approved by rule: %s', rule.name) if rule
                                     else _('Auto-approved by default policy'),
                })
            mail.sudo().write(vals)
            if rule:
                matched_counts[rule.id] = matched_counts.get(rule.id, 0) + 1

        for rule_id, count in matched_counts.items():
            rule = rules.browse(rule_id)
            rule.sudo().match_count = rule.match_count + count

    # ------------------------------------------------------------------
    # the gate
    # ------------------------------------------------------------------
    def send(self, auto_commit=False, raise_exception=False):
        """Only let approved mail reach the real sender.

        ``send()`` is the single method both ``process_email_queue`` (the queue
        cron) and every ``force_send=True`` template send funnel through, so
        gating here covers all send paths without patching each one.
        """
        if not self._mma_enabled():
            return super().send(auto_commit=auto_commit, raise_exception=raise_exception)

        try:
            self._mma_apply_rules()
        except Exception:
            # A classifier crash must not become an accidental send.
            _logger.exception(
                "mint_mail_approval: classification failed for ids=%s — holding all",
                self.ids,
            )
            return True

        rejected = self.filtered(lambda m: m.approval_state == 'rejected')
        if rejected:
            rejected.sudo().write({
                'state': 'cancel',
                'failure_reason': _('mint_mail_approval: rejected by approver'),
            })

        held = self.filtered(lambda m: m.approval_state == 'pending')
        if held:
            _logger.info(
                "mint_mail_approval: holding %d mail(s) for approval: %s",
                len(held), held.ids,
            )

        allowed = self - held - rejected
        if not allowed:
            return True
        return super(MailMail, allowed).send(
            auto_commit=auto_commit, raise_exception=raise_exception
        )

    # ------------------------------------------------------------------
    # approver actions
    # ------------------------------------------------------------------
    def _mma_check_approver(self):
        if not self.env.user.has_group('mint_mail_approval.group_mail_approver'):
            raise UserError(_("You are not allowed to approve or reject outgoing email."))

    def action_mma_approve(self):
        """Approve and immediately attempt delivery."""
        self._mma_check_approver()
        self.sudo().write({
            'approval_state': 'approved',
            'approval_uid': self.env.uid,
            'approval_date': fields.Datetime.now(),
        })
        # A previously failed or cancelled mail has to go back on the queue,
        # otherwise the sender skips it.
        requeue = self.filtered(lambda m: m.state in ('exception', 'cancel'))
        if requeue:
            requeue.sudo().write({'state': 'outgoing', 'failure_reason': False})
        self.sudo().send()
        return True

    def action_mma_reject(self):
        self._mma_check_approver()
        self.sudo().write({
            'approval_state': 'rejected',
            'approval_uid': self.env.uid,
            'approval_date': fields.Datetime.now(),
            'state': 'cancel',
            'failure_reason': _('mint_mail_approval: rejected by approver'),
        })
        return True

    # ------------------------------------------------------------------
    # housekeeping
    # ------------------------------------------------------------------
    @api.model
    def _mma_cron_expire_pending(self):
        """Cancel review requests nobody acted on, so the queue stays finite."""
        days = int(self.env['ir.config_parameter'].sudo()
                   .get_param(PARAM_EXPIRE_DAYS, '7') or 7)
        if days <= 0:
            return
        cutoff = fields.Datetime.subtract(fields.Datetime.now(), days=days)
        stale = self.sudo().search([
            ('approval_state', '=', 'pending'),
            ('create_date', '<', cutoff),
            ('state', '!=', 'sent'),
        ])
        if not stale:
            return
        _logger.info(
            "mint_mail_approval: expiring %d mail(s) pending longer than %d day(s)",
            len(stale), days,
        )
        stale.write({
            'approval_state': 'rejected',
            'state': 'cancel',
            'failure_reason': _('mint_mail_approval: expired unapproved after %s day(s)', days),
        })
