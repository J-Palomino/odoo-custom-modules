import os

from markupsafe import Markup

from odoo import models

_BOT_NAME = os.environ.get('BRAND_BOT_NAME', 'MintBot')
_BRAND_NAME = os.environ.get('BRAND_NAME', 'Mint')


class MailBotDaisy(models.AbstractModel):
    _inherit = 'mail.bot'

    def _get_answer(self, channel, body, values, command=False):
        answer = super()._get_answer(channel, body, values, command=command)
        if answer:
            if isinstance(answer, list):
                return [self._rebrand_bot_text(a) for a in answer]
            return self._rebrand_bot_text(answer)
        return answer

    @staticmethod
    def _rebrand_bot_text(text):
        replacements = [
            ('@OdooBot', f'@{_BOT_NAME}'),
            ('OdooBot', _BOT_NAME),
            ('exploring Odoo', f'exploring {_BRAND_NAME}'),
            ("Odoo's chat", f"{_BRAND_NAME}'s chat"),
            ('our product', 'our platform'),
        ]
        if isinstance(text, (Markup, str)):
            result = str(text)
            for old, new in replacements:
                result = result.replace(old, new)
            return Markup(result) if isinstance(text, Markup) else result
        return text
