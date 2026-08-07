# -*- coding: utf-8 -*-
import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class MintMailApprovalRule(models.Model):
    """Ordered classification rules deciding whether a mail may auto-send.

    Rules are evaluated by ``sequence`` ascending; the FIRST match wins. A mail
    matching no rule falls back to ``mint_mail_approval.default_action``, which
    ships as ``hold`` so the gate is fail-closed.
    """

    _name = 'mint.mail.approval.rule'
    _description = 'Outgoing Mail Approval Rule'
    _order = 'sequence, id'

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    action = fields.Selection(
        [('auto', 'Auto-approve (send without review)'),
         ('hold', 'Hold for approval')],
        required=True,
        default='hold',
        help="What to do with a mail matching every criterion below.",
    )

    # --- match criteria. Empty means "do not test this criterion". ---
    match_model = fields.Char(
        string='Source Model',
        help="Technical model that generated the mail, e.g. res.users, "
             "maintenance.request. Empty matches any model.",
    )
    match_subject = fields.Char(
        string='Subject Regex',
        help="Python regex tested case-insensitively against the subject. "
             "Empty matches any subject.",
    )
    match_author_id = fields.Many2one(
        'res.partner', string='Author',
        help="Only match mails authored by this partner. Empty matches any author.",
    )
    match_recipient_domains = fields.Char(
        string='Recipient Domains',
        help="Comma-separated domains, e.g. letsgomint.com,brightroot.com. "
             "Matches if ANY recipient is on one of these domains. "
             "Empty matches any recipient.",
    )

    note = fields.Text(help="Why this rule exists — shown to approvers for context.")
    match_count = fields.Integer(
        string='Times Matched', default=0, readonly=True, copy=False,
        help="How often this rule has classified a mail. Useful for spotting dead rules.",
    )

    @api.constrains('match_subject')
    def _check_match_subject(self):
        for rule in self:
            if not rule.match_subject:
                continue
            try:
                re.compile(rule.match_subject)
            except re.error as exc:
                raise ValidationError(
                    _("Rule %(name)s: invalid subject regex — %(err)s",
                      name=rule.name, err=exc)
                ) from exc

    # ------------------------------------------------------------------
    # matching
    # ------------------------------------------------------------------
    def _matches(self, mail, recipient_domains):
        """Return True if ``mail`` satisfies every populated criterion."""
        self.ensure_one()

        if self.match_model and self.match_model.strip() != (mail.model or ''):
            return False

        if self.match_subject:
            try:
                if not re.search(self.match_subject, mail.subject or '', re.IGNORECASE):
                    return False
            except re.error:
                # A malformed regex must never let a mail through the gate.
                _logger.warning(
                    "mint_mail_approval: rule %s has an invalid regex; treating as no-match",
                    self.name,
                )
                return False

        if self.match_author_id and mail.author_id.id != self.match_author_id.id:
            return False

        if self.match_recipient_domains:
            wanted = {
                d.strip().lower()
                for d in self.match_recipient_domains.split(',')
                if d.strip()
            }
            if wanted and not (recipient_domains & wanted):
                return False

        return True

    def _first_match(self, mail, recipient_domains):
        """Return the first rule in ``self`` matching ``mail``, else empty.

        ``self`` is expected to be pre-sorted by ``sequence`` (the model's
        ``_order``) and fetched once by the caller, so classifying a batch of
        mails costs one query rather than one per mail.
        """
        for rule in self:
            if rule._matches(mail, recipient_domains):
                return rule
        return self.browse()
