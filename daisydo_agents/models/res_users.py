# -*- coding: utf-8 -*-
from odoo import fields, models


class ResUsers(models.Model):
    _inherit = "res.users"

    daisy_autorespond_when_away = fields.Boolean(
        string="AI auto-respond when I'm away",
        default=False,
        help="When enabled, your Daisy AI agent answers your direct messages on "
        "your behalf whenever your presence is Away or Offline. While you are "
        "Online, your direct messages are never auto-answered.",
    )

    # Let users toggle this on their OWN record from Preferences.
    @property
    def SELF_READABLE_FIELDS(self):
        return super().SELF_READABLE_FIELDS + ["daisy_autorespond_when_away"]

    @property
    def SELF_WRITEABLE_FIELDS(self):
        return super().SELF_WRITEABLE_FIELDS + ["daisy_autorespond_when_away"]
