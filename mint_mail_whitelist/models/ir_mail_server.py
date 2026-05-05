# -*- coding: utf-8 -*-
import email.utils
import logging

from odoo import api, models

_logger = logging.getLogger(__name__)

DEFAULT_ALLOWED_DOMAINS = (
    'letsgomint.com letsgomint.us themintdispensary.com '
    'themintmi.com themintmo.com themintnv.com brightroot.com'
)


class IrMailServer(models.Model):
    _inherit = 'ir.mail_server'

    @api.model
    def _mmw_allowed_recipients(self):
        ICP = self.env['ir.config_parameter'].sudo()

        allow_internal = ICP.get_param('mint_mail_whitelist.allow_internal_users', '1') == '1'
        domains_str = ICP.get_param('mint_mail_whitelist.allowed_domains', DEFAULT_ALLOWED_DOMAINS)
        emails_str = ICP.get_param('mint_mail_whitelist.allowed_emails', '')

        allowed_domains = {d.strip().lower().lstrip('@')
                           for d in domains_str.replace(',', ' ').split()
                           if d.strip()}
        allowed_emails = {e.strip().lower()
                          for e in emails_str.replace(',', ' ').split()
                          if e.strip()}

        if allow_internal:
            internal_partners = self.env['res.users'].sudo().search(
                [('share', '=', False)]
            ).partner_id
            for p in internal_partners:
                if p.email:
                    allowed_emails.add(p.email.strip().lower())

        return allowed_emails, allowed_domains

    @api.model
    def send_email(self, message, mail_server_id=None, smtp_server=None,
                   smtp_port=None, smtp_user=None, smtp_password=None,
                   smtp_encryption=None, smtp_ssl_certificate=None,
                   smtp_ssl_private_key=None, smtp_debug=False,
                   smtp_session=None):
        allowed_emails, allowed_domains = self._mmw_allowed_recipients()

        if not allowed_emails and not allowed_domains:
            _logger.warning(
                "mint_mail_whitelist: empty allowlist, blocking all outgoing email"
            )
            return message['Message-Id']

        for header in ('To', 'Cc', 'Bcc'):
            raw = message.get(header)
            if not raw:
                continue
            blocked = []
            for _name, addr in email.utils.getaddresses([str(raw)]):
                a = (addr or '').strip().lower()
                if not a:
                    continue
                dom = a.rsplit('@', 1)[-1] if '@' in a else ''
                if a not in allowed_emails and dom not in allowed_domains:
                    blocked.append(addr)
            if blocked:
                _logger.warning(
                    "mint_mail_whitelist: blocked email (header=%s) to %s "
                    "— not internal/allowlisted",
                    header, ', '.join(blocked),
                )
                return message['Message-Id']

        return super().send_email(
            message,
            mail_server_id=mail_server_id,
            smtp_server=smtp_server,
            smtp_port=smtp_port,
            smtp_user=smtp_user,
            smtp_password=smtp_password,
            smtp_encryption=smtp_encryption,
            smtp_ssl_certificate=smtp_ssl_certificate,
            smtp_ssl_private_key=smtp_ssl_private_key,
            smtp_debug=smtp_debug,
            smtp_session=smtp_session,
        )
