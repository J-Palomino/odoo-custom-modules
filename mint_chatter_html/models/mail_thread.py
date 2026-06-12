import re

from markupsafe import Markup

from odoo import models

# Matches a real opening/closing/self-closing HTML tag: "<" (optionally "/")
# IMMEDIATELY followed by a tag name (no space), then optional well-formed
# attributes, then ">". The no-space-after-"<" rule plus structured attributes
# keep plain-text comparisons like "a < b and c > d" or "stock < 5 units" from
# being mistaken for HTML (those have a space after "<").
_HTML_TAG_RE = re.compile(
    r"<\/?[a-zA-Z][a-zA-Z0-9]*"
    r"(?:\s+[a-zA-Z-]+(?:=(?:\"[^\"]*\"|'[^']*'|[^\s>]+))?)*"
    r"\s*\/?>",
    re.S,
)


class MailThread(models.AbstractModel):
    _inherit = "mail.thread"

    def message_post(self, *, body="", **kwargs):
        """Preserve HTML in a plain-string ``body``.

        Core's ``message_post`` sends a ``str`` body through
        ``plaintext2html()``, which escapes ``<``/``>``. Over RPC the body can
        only ever be a ``str``, so HTML chatter shows raw tags. We wrap a
        plain string that actually contains HTML tags in ``markupsafe.Markup``
        so core's ``html_sanitize`` keeps the (safe) formatting tags instead of
        escaping them.

        Left untouched:
        - ``Markup`` bodies (already correct — callers that did the right thing).
        - Empty / falsy bodies.
        - Plain text with no HTML tags (still escaped harmlessly by core; e.g.
          ``"a < b"`` stays literal rather than being interpreted as a tag).
        """
        if (
            body
            and isinstance(body, str)
            and not isinstance(body, Markup)
            and _HTML_TAG_RE.search(body)
        ):
            body = Markup(body)
        return super().message_post(body=body, **kwargs)
