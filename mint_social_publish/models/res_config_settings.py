# -*- coding: utf-8 -*-
from odoo import _, fields, models
from odoo.exceptions import UserError


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    social_api_base = fields.Char(
        string="posts.agency API Base URL",
        config_parameter="mint_social_publish.api_base",
        default="https://api.upload-post.com",
    )
    social_api_key = fields.Char(
        string="posts.agency API Key",
        config_parameter="mint_social_publish.api_key",
        help="API key for the posts.agency publishing account.",
    )
    social_project_id = fields.Integer(
        string="Social Media Project ID",
        config_parameter="mint_social_publish.project_id",
        help="ID of the project.project whose cards publish to posts.agency.",
    )
    social_scheduled_stage_id = fields.Integer(
        string="Scheduled Stage ID",
        config_parameter="mint_social_publish.scheduled_stage_id",
        help="Moving a card into this stage queues it for publishing.",
    )
    social_max_retries = fields.Integer(
        string="Max Publish Retries",
        config_parameter="mint_social_publish.max_retries",
        default=3,
    )

    def action_social_test_connection(self):
        """Ping posts.agency with the key currently entered in the form.

        Tests the typed value (sent with the button call) — no need to Save first.
        """
        self.ensure_one()
        ok, message = self.env["project.task"]._social_check_credentials(
            self.social_api_base, self.social_api_key
        )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("posts.agency"),
                "message": message,
                "type": "success" if ok else "danger",
                "sticky": not ok,
            },
        }

    def action_social_refresh_accounts(self):
        """Pull the connected-accounts list from posts.agency using the key
        currently in the form (no Save required)."""
        self.ensure_one()
        try:
            count = self.env["mint.social.profile"].sync_from_posts_agency(
                self.social_api_base, self.social_api_key
            )
        except UserError as exc:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("posts.agency"),
                    "message": exc.args[0] if exc.args else _("Refresh failed."),
                    "type": "danger",
                    "sticky": True,
                },
            }
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("posts.agency"),
                "message": _("Synced %s account(s). Connected accounts are now "
                             "available in the card's account dropdown.") % count,
                "type": "success",
            },
        }
