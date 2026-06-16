import logging
import os

from markupsafe import Markup

from odoo import fields, models, _
from odoo.exceptions import AccessDenied

_logger = logging.getLogger(__name__)
_BOT_NAME = os.environ.get('BRAND_BOT_NAME', 'MintBot')


class ResUsersDaisy(models.Model):
    _inherit = 'res.users'

    # Dash-separated company ids (e.g. "1-3-5") the user last had active in the
    # multi-company switcher. Odoo clears the `cids` cookie on logout, so this
    # field lets us restore the selection on the next login. Maintained
    # automatically from the `cids` cookie in ir.http.session_info; not shown in
    # any view and always read/written via sudo().
    last_company_cids = fields.Char(
        string="Last Selected Companies",
        copy=False,
        help="Internal: remembers the multi-company switcher selection so it "
             "survives logout/login.",
    )

    def _auth_oauth_signin(self, provider, validation, params):
        """Override to link existing users by email on first OAuth login."""
        oauth_uid = validation['user_id']
        # First try the standard flow (find by oauth_uid)
        oauth_user = self.search([
            ("oauth_uid", "=", oauth_uid),
            ('oauth_provider_id', '=', provider),
        ])
        if oauth_user:
            oauth_user.write({'oauth_access_token': params['access_token']})
            return oauth_user.login

        # No user with this oauth_uid — look for existing user by email/login
        email = validation.get('email', '').strip().lower()
        if email:
            existing_user = self.sudo().search([
                ('login', '=ilike', email),
                ('active', '=', True),
            ], limit=1)
            if existing_user:
                # Link the existing user to this OAuth provider
                existing_user.sudo().write({
                    'oauth_provider_id': provider,
                    'oauth_uid': oauth_uid,
                    'oauth_access_token': params['access_token'],
                })
                _logger.info(
                    "OAuth: linked existing user %s (id=%s) to provider %s",
                    existing_user.login, existing_user.id, provider,
                )
                return existing_user.login

        # Fall back to standard behavior (attempt signup or raise AccessDenied)
        return super()._auth_oauth_signin(provider, validation, params)

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
