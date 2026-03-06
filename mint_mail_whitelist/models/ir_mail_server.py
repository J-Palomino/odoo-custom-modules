# -*- coding: utf-8 -*-
import email.utils
import logging

from odoo import api, models

_logger = logging.getLogger(__name__)


class IrMailServer(models.Model):
    _inherit = 'ir.mail_server'

    @api.model
    def send_email(self, message, mail_server_id=None, smtp_server=None,
                   smtp_port=None, smtp_user=None, smtp_password=None,
                   smtp_encryption=None, smtp_debug=False,
                   smtp_session=None):
        whitelist_str = self.env['ir.config_parameter'].sudo().get_param(
            'mint_mail_whitelist.allowed_emails', ''
        )
        if not whitelist_str or not whitelist_str.strip():
            _logger.warning("mint_mail_whitelist: no whitelist configured, blocking all outgoing email")
            return message['Message-Id']

        allowed = {e.strip().lower() for e in whitelist_str.split(',') if e.strip()}

        for header in ('To', 'Cc', 'Bcc'):
            raw = message.get(header)
            if not raw:
                continue
            addresses = email.utils.getaddresses([raw])
            blocked = [addr for _name, addr in addresses if addr.lower() not in allowed]
            if blocked:
                _logger.warning(
                    "mint_mail_whitelist: blocked email to %s (not in whitelist)",
                    ', '.join(blocked),
                )
                return message['Message-Id']

        return super().send_email(
            message, mail_server_id=mail_server_id,
            smtp_server=smtp_server, smtp_port=smtp_port,
            smtp_user=smtp_user, smtp_password=smtp_password,
            smtp_encryption=smtp_encryption, smtp_debug=smtp_debug,
            smtp_session=smtp_session,
        )
