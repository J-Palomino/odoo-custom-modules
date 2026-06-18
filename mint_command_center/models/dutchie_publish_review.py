"""Pre-publish review wizard — guards + alerts before a Dutchie write.

Both explicit "publish to Dutchie" buttons (the PTL deal Publish button and the
submission Publish-to-Dutchie button) open THIS wizard instead of writing
directly. It shows the resolved push mode, the exact stores / LocIds that will
receive the discount, and any SOP/safety alerts (store-wide, missing brand id,
past/future dates, skipped stores, fail-open mode, …).

WARN-ONLY by design: nothing is blocked. The user reviews, then confirms, and
the real publish worker fires. The per-surface review data is computed by
`_dutchie_publish_review_data()` on each model (mint.ptl.deal /
mint.deal.submission), so this wizard stays generic.
"""
from markupsafe import Markup, escape

from odoo import fields, models
from odoo.exceptions import UserError


def build_review_html(mode, is_live, lines, warnings, blocks):
    """Render the review as chatter-style HTML. lines/warnings/blocks are
    plain strings — all escaped here, so callers never inject HTML."""
    banner_txt = ('⚠ LIVE — confirming WILL write to real Dutchie POS'
                  if is_live else
                  'DRY-RUN — payload is built & logged, nothing is sent to Dutchie')
    bg, fg = ('#b00020', '#ffffff') if is_live else ('#e3f2fd', '#0d47a1')
    out = Markup('<div style="padding:8px 12px;margin-bottom:10px;border-radius:4px;'
                 'background:%s;color:%s;font-weight:bold">%s</div>') % (bg, fg, banner_txt)

    def ul(items):
        return (Markup('<ul style="margin:4px 0">')
                + Markup('').join(Markup('<li>%s</li>') % escape(i) for i in items)
                + Markup('</ul>'))

    if lines:
        out += ul(lines)
    if blocks:
        out += (Markup('<div style="color:#b00020"><b>Will FAIL to publish:</b>')
                + ul(blocks) + Markup('</div>'))
    if warnings:
        out += (Markup('<div style="color:#b26a00"><b>⚠ %d alert(s) — review before confirming:</b>') % len(warnings)
                + ul(warnings) + Markup('</div>'))
    elif not blocks:
        out += Markup('<div style="color:#1b5e20"><b>✓ No safety alerts.</b></div>')
    return out


class DutchiePublishReview(models.TransientModel):
    _name = 'mint.dutchie.publish.review'
    _description = 'Pre-publish review (guards + alerts) before a Dutchie write'

    res_model = fields.Char(required=True)
    res_id = fields.Integer(required=True)
    mode = fields.Char(readonly=True)
    is_live = fields.Boolean(readonly=True)
    review_html = fields.Html(readonly=True, sanitize=False)
    requires_ack = fields.Boolean(
        readonly=True,
        help="Set when this deal overlaps an active deal's Dutchie scope — "
             "the reviewer must explicitly acknowledge before publishing.")
    overlap_ack = fields.Boolean(
        string="I reviewed the overlap and want to publish anyway")

    def action_confirm(self):
        """Fire the real publish for the originating surface."""
        self.ensure_one()
        if self.requires_ack and not self.overlap_ack:
            raise UserError(
                "This deal overlaps an active deal's Dutchie scope (see the "
                "alerts above) — publishing will create a duplicate discount "
                "on the same items. Tick “I reviewed the overlap and want to "
                "publish anyway” to proceed, or Cancel.")
        record = self.env[self.res_model].browse(self.res_id)
        if self.res_model == 'mint.ptl.deal':
            return record.action_publish()
        if self.res_model == 'mint.deal.submission':
            return record.action_publish_to_dutchie()
        return False
