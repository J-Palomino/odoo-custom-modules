# -*- coding: utf-8 -*-
from odoo import models


class ResUsers(models.Model):
    _inherit = 'res.users'

    def _get_session_token_fields(self):
        """Keep OAuth (Google SSO) sessions alive across token rotation.

        Stock ``auth_oauth`` adds ``oauth_access_token`` to the set of fields
        that make up the HMAC session token (see
        ``auth_oauth/models/res_users.py``). Because ``oauth_access_token`` is a
        SINGLE field per user that is overwritten on every Google sign-in, two
        problems follow:

          * Logging in a second session (other device / tab / shared account)
            rewrites the field, so EVERY existing session of that user fails
            ``_compute_session_token`` validation on its next request and is
            logged out — an accidental "one session per user" block.
          * Google access tokens expire after ~3600s; when the client
            re-authenticates at that point the field is rewritten and the live
            session is logged out ~1 hour after login.

        Dropping ``oauth_access_token`` from the token decouples Odoo session
        validity from the volatile OAuth access token, allowing concurrent
        sessions and surviving token refresh. Trade-off: revoking the token at
        the provider no longer instantly invalidates the Odoo session — it
        lives until normal session expiry. Acceptable for the staff backend.
        """
        return super()._get_session_token_fields() - {'oauth_access_token'}
