# -*- coding: utf-8 -*-
{
    "name": "Mint OAuth2 / OIDC Provider",
    "version": "19.0.1.0.0",
    "category": "Technical",
    "summary": "Makes Odoo an OpenID Connect identity provider for internal apps",
    "description": """
Mint OAuth2 / OIDC Provider
===========================

Turns this Odoo instance into an OpenID Connect identity provider so internal
applications can authenticate users against Odoo rather than holding their own
passwords. Built for Penpot (ux.letsgomint.us); other relying parties can be
registered without code changes.

Note the direction of travel: Odoo's own `auth_oauth` makes Odoo an OAuth
*client*. This module is the opposite — it makes Odoo the *server* that issues
tokens.

Because /oauth2/authorize requires an Odoo session, login chains through
whatever Odoo itself is configured to use (currently Google), so this inherits
the existing SSO posture rather than introducing a second one.

Supported: Authorization Code flow with mandatory PKCE (S256), refresh tokens,
RS256-signed ID tokens, and rotating JWKS.
    """,
    "author": "Mint Cannabis",
    "website": "https://letsgomint.us",
    "license": "LGPL-3",
    "depends": ["base", "web"],
    "external_dependencies": {"python": ["jwt", "cryptography"]},
    "data": [
        "security/oauth_groups.xml",
        "security/ir.model.access.csv",
        "views/oauth_client_views.xml",
        "views/oauth_keypair_views.xml",
        "views/menus.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}
