import os

from markupsafe import Markup

from odoo import models, _

_BOT_NAME = os.environ.get('BRAND_BOT_NAME', 'DaisyBot')


class ResUsersDaisy(models.Model):
    _inherit = 'res.users'

    def _init_odoobot(self):
        self.ensure_one()
        odoobot_id = self.env['ir.model.data']._xmlid_to_res_id("base.partner_root")
        channel = self.env['discuss.channel']._get_or_create_chat([odoobot_id, self.partner_id.id])
        message = Markup("%s<br/>%s<br/><b>%s</b> <span class=\"o_odoobot_command\">:)</span>") % (
            _("Hello,"),
            _(f"{_BOT_NAME} helps your team collaborate efficiently. I'm here to help you discover chat features."),
            _("Try to send me an emoji"),
        )
        channel.sudo().message_post(
            author_id=odoobot_id,
            body=message,
            message_type="comment",
            silent=True,
            subtype_xmlid="mail.mt_comment",
        )
        self.sudo().odoobot_state = 'onboarding_emoji'
        return channel
