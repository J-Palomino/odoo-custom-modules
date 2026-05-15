# -*- coding: utf-8 -*-
import email.utils
import logging

from odoo import api, models

_logger = logging.getLogger(__name__)


class MailMail(models.Model):
    _inherit = 'mail.mail'

    def _mmw_filter_recipients(self):
        """Strip non-whitelisted recipients from this mail.mail at create-time.

        If nothing remains after filtering, set state='cancel' so the send
        cron never picks the row up and no SMTP attempt is made.
        """
        allowed_emails, allowed_domains = self.env['ir.mail_server']._mmw_allowed_recipients()

        # Fail closed: empty allowlist means block everything.
        if not allowed_emails and not allowed_domains:
            to_cancel = self.filtered(lambda m: m.state in ('outgoing', False))
            if to_cancel:
                _logger.warning(
                    "mint_mail_whitelist: empty allowlist — cancelling %d mail(s)",
                    len(to_cancel),
                )
                to_cancel.write({
                    'state': 'cancel',
                    'failure_reason': 'mint_mail_whitelist: empty allowlist',
                })
            return

        def is_allowed(addr):
            a = (addr or '').strip().lower()
            if not a:
                return False
            dom = a.rsplit('@', 1)[-1] if '@' in a else ''
            return a in allowed_emails or dom in allowed_domains

        for mail in self:
            blocked = []

            # Partner-based recipients
            if mail.recipient_ids:
                keep = mail.recipient_ids.filtered(lambda p: is_allowed(p.email))
                drop = mail.recipient_ids - keep
                if drop:
                    blocked.extend(
                        f"{p.name or ''} <{p.email or ''}>".strip()
                        for p in drop
                    )
                    mail.recipient_ids = [(6, 0, keep.ids)]

            # Raw string recipients
            for field in ('email_to', 'email_cc'):
                raw = mail[field]
                if not raw:
                    continue
                kept = []
                for name, addr in email.utils.getaddresses([raw]):
                    if is_allowed(addr):
                        kept.append(
                            email.utils.formataddr((name, addr)) if name else addr
                        )
                    else:
                        blocked.append(
                            email.utils.formataddr((name, addr)) if name else addr
                        )
                mail[field] = ', '.join(kept) if kept else False

            if blocked:
                _logger.warning(
                    "mint_mail_whitelist: filtered %d recipient(s) from mail.mail #%s (%s): %s",
                    len(blocked), mail.id, mail.subject, blocked,
                )

            if not (mail.recipient_ids or mail.email_to or mail.email_cc):
                mail.write({
                    'state': 'cancel',
                    'failure_reason': 'mint_mail_whitelist: all recipients blocked at create',
                })

    @api.model_create_multi
    def create(self, vals_list):
        mails = super().create(vals_list)
        try:
            mails._mmw_filter_recipients()
        except Exception:
            _logger.exception(
                "mint_mail_whitelist: filter failed for mail.mail ids=%s",
                mails.ids,
            )
        return mails
