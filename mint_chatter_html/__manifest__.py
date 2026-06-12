{
    'name': 'Mint Chatter HTML',
    'version': '19.0.1.0.0',
    'summary': 'Preserve HTML in automated chatter posts (sitewide message_post fix)',
    'description': """
Mint Chatter HTML
=================

Odoo's ``mail.thread.message_post(body=...)`` runs a plain ``str`` body through
``plaintext2html()``, which HTML-escapes ``<``/``>`` — so an HTML body like
``<p><b>x</b></p>`` is stored as ``&lt;p&gt;&lt;b&gt;x&lt;/b&gt;&lt;/p&gt;`` and
the chatter feed shows the raw tags. HTML is only preserved when ``body`` is a
``markupsafe.Markup`` instance, which is impossible to send over XML-RPC /
JSON-RPC (the wire only carries a string).

This module overrides ``message_post`` on ``mail.thread`` to wrap a plain-string
body that *contains HTML tags* in ``Markup`` before delegating to core. This
fixes EVERY caller at once — in-module Python, JSON-RPC scripts, the k8s ``.mjs``
posters, and the FastAPI MCP — including future callers, with no per-caller
changes. Genuine plain text (no tags) is left untouched and still renders fine.
Core's ``html_sanitize`` still strips dangerous markup, so this only re-enables
the safe formatting tags chatter already supports.
""",
    'author': 'Mint Cannabis',
    'category': 'Discuss',
    'depends': ['mail'],
    'data': [],
    'installable': True,
    'application': False,
    'auto_install': True,
    'license': 'LGPL-3',
}
