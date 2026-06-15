# -*- coding: utf-8 -*-
{
    "name": "Mint Discuss — /here URL Command",
    "version": "19.0.1.0.0",
    "summary": "Adds a /here powerbox command to the Discuss composer that "
               "inserts the current page URL inline at the cursor.",
    "description": """
Mint Discuss — /here URL Command
================================

Adds a powerbox command to the Discuss / chatter message composer. Type ``/``
to open the command menu, filter with ``here`` (also matches ``url`` /
``link`` / ``location``), and pick *Current URL* to insert the page's current
URL inline at the cursor — the same ``/`` menu the built-in commands use.

Implemented as an ``@html_editor`` powerbox plugin appended to the composer's
Wysiwyg config (scoped to the Discuss/chatter composer only). No server-side
component — the current URL is read client-side from ``location.href``.
""",
    "category": "Productivity/Discuss",
    "author": "Mint Cannabis",
    "license": "LGPL-3",
    "depends": ["mail"],
    "assets": {
        "web.assets_backend": [
            "mint_discuss_here/static/src/here_url_plugin.js",
        ],
    },
    "installable": True,
    "application": False,
    "auto_install": False,
}
