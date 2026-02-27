import logging
import re

from odoo import models

_logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


class MailMail(models.Model):
    _inherit = "mail.mail"

    def send(self, auto_commit=False, raise_exception=False):
        whitelist_param = (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("mail.outgoing.whitelist", "")
        )
        if not whitelist_param:
            return super().send(
                auto_commit=auto_commit, raise_exception=raise_exception
            )

        whitelist = {
            e.strip().lower() for e in whitelist_param.split(",") if e.strip()
        }

        to_send = self.env["mail.mail"]
        to_cancel = self.env["mail.mail"]

        for mail in self:
            recipients = set()
            if mail.email_to:
                recipients.update(e.lower() for e in EMAIL_RE.findall(mail.email_to))
            if mail.email_cc:
                recipients.update(e.lower() for e in EMAIL_RE.findall(mail.email_cc))
            for partner in mail.recipient_ids:
                if partner.email:
                    recipients.update(
                        e.lower() for e in EMAIL_RE.findall(partner.email)
                    )

            if recipients.issubset(whitelist):
                to_send |= mail
            else:
                to_cancel |= mail

        if to_cancel:
            _logger.info(
                "Mail whitelist: cancelling %d email(s) — recipients not in %s",
                len(to_cancel),
                whitelist,
            )
            to_cancel.write({"state": "cancel"})

        if to_send:
            return super(MailMail, to_send).send(
                auto_commit=auto_commit, raise_exception=raise_exception
            )
        return True
