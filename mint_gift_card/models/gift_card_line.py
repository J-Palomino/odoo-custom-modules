# -*- coding: utf-8 -*-
"""One draw against a gift card — the append-only truth behind the balance.

A line is created the moment a draw is applied to a basket (`held`) and reaches
one of three terminal states:

    settled   Dutchie report 10875 confirmed the dollars. `settled_amount` is
              what the register actually took, which can be LESS than we drew
              if the basket shrank between apply and checkout.
    released  Nothing ever confirmed it. The money goes back to the card.
    failed    The draw could not be applied at all (mint or apply errored).
              Distinct from `released` on purpose: released is a normal
              outcome, failed is one worth looking at.

Lines are never deleted and never re-opened. A correction is a new line, so the
history of a card always reconstructs its balance.
"""
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class MintGiftCardLine(models.Model):
    _name = "mint.gift.card.line"
    _description = "Gift Card Draw"
    _order = "held_at desc, id desc"

    card_id = fields.Many2one(
        "mint.gift.card", string="Gift Card", required=True,
        ondelete="cascade", index=True,
    )
    currency_id = fields.Many2one(
        related="card_id.currency_id", string="Currency", store=True, readonly=True,
    )
    company_id = fields.Many2one(
        related="card_id.company_id", string="Company", store=True, readonly=True,
    )

    state = fields.Selection(
        [
            ("held", "Held"),
            ("settled", "Settled"),
            ("released", "Released"),
            ("failed", "Failed"),
        ],
        string="Status", required=True, default="held", index=True,
    )

    draw_amount = fields.Monetary(
        string="Drawn", required=True, currency_field="currency_id",
        help="What we asked the card for — min(balance, basket total) at the "
             "moment of apply.",
    )
    settled_amount = fields.Monetary(
        string="Settled", currency_field="currency_id",
        help="What Dutchie actually discounted, summed from report 10875's "
             "`Discounted Amount` over every line of the order. May be less "
             "than drawn if the basket changed after the coupon went on.",
    )

    # ── The minted child coupon ─────────────────────────────────────────
    # Each draw gets its OWN single-use Dutchie coupon. That is what makes
    # settlement exact: report 10875 attributes one code to one draw, with no
    # disentangling of several redemptions sharing an identifier.
    child_discount_id = fields.Many2one(
        "mint.discount", string="Child Coupon", ondelete="set null", index=True,
        help="The one-shot Dutchie coupon minted for this draw. Deleted in "
             "Dutchie after settlement — 75th Ave already carries 521 "
             "discounts and that list is pulled on every sync, so leaving "
             "spent children behind would degrade the discount pipeline.",
    )
    child_code = fields.Char(
        string="Child Code", index=True, copy=False,
        help="The code actually presented to the register. NOT the card code — "
             "the card code never reaches Dutchie.",
    )
    dutchie_discount_id = fields.Char(string="Dutchie Discount ID", index=True)

    # ── Where it was drawn ──────────────────────────────────────────────
    shipment_id = fields.Char(string="Shipment ID", index=True)
    loc_id = fields.Integer(string="Dutchie LocId")
    lsp_id = fields.Integer(string="Dutchie LspId")
    order_id = fields.Char(
        string="Dutchie Order ID", index=True,
        help="From report 10875. One redemption emits one report row per "
             "discounted LINE, so the unit of a draw is a distinct Order ID.",
    )

    idempotency_key = fields.Char(
        string="Idempotency Key", index=True, copy=False,
        help="Scoped per (card, key). A retry or an impatient double-tap "
             "returns the original hold instead of drawing twice.",
    )

    held_at = fields.Datetime(string="Held At", default=fields.Datetime.now, index=True)
    settled_at = fields.Datetime(string="Settled At")
    released_at = fields.Datetime(string="Released At")
    note = fields.Text(string="Notes")

    _idempotency_uniq = models.Constraint(
        "UNIQUE(card_id, idempotency_key)",
        "This draw has already been recorded for that card.",
    )
    _draw_amount_positive = models.Constraint(
        "CHECK(draw_amount > 0)",
        "A draw must be for a positive amount.",
    )

    @api.depends("card_id.code", "draw_amount", "state")
    def _compute_display_name(self):
        for line in self:
            line.display_name = "%s · %s (%s)" % (
                line.card_id.code or "—", line.draw_amount, line.state,
            )

    # ── Terminal transitions ────────────────────────────────────────────
    def action_settle(self, amount, order_id=None):
        """Confirm a hold against what Dutchie's report actually shows."""
        self.ensure_one()
        if self.state != "held":
            raise UserError(_(
                "Only a held draw can be settled (this one is %s).", self.state))
        currency = self.currency_id
        if currency.compare_amounts(amount, 0.0) < 0:
            raise UserError(_("A settlement cannot be negative."))
        if currency.compare_amounts(amount, self.draw_amount) > 0:
            # Dutchie reporting more than we drew means the code was applied
            # somewhere we did not authorise. Refuse rather than absorb it: a
            # silent over-settle would quietly drain the card.
            raise UserError(_(
                "Report shows %(shown)s settled against a %(drawn)s draw on "
                "card %(code)s — refusing to settle more than was drawn.",
                shown=amount, drawn=self.draw_amount, code=self.card_id.code,
            ))
        self.write({
            "state": "settled",
            "settled_amount": currency.round(amount),
            "order_id": order_id or self.order_id,
            "settled_at": fields.Datetime.now(),
        })
        self.card_id.message_post(body=_(
            "Settled %(amount)s on order %(order)s (drew %(drawn)s).",
            amount=amount, order=order_id or "—", drawn=self.draw_amount,
        ))
        self.card_id._sync_depleted()
        return True

    def action_release(self, reason=None):
        """Return a hold to the card. Normal outcome for an abandoned basket."""
        for line in self:
            if line.state != "held":
                continue
            line.write({
                "state": "released",
                "released_at": fields.Datetime.now(),
                "note": reason or line.note,
            })
            line.card_id.message_post(body=_(
                "Released a %(amount)s hold back to the card. %(reason)s",
                amount=line.draw_amount, reason=reason or "",
            ))
            line.card_id._sync_depleted()
        return True

    def action_fail(self, reason=None):
        """Mark a draw that could not be applied. Frees the balance like a
        release, but stays distinguishable in the audit trail."""
        for line in self:
            if line.state != "held":
                continue
            line.write({
                "state": "failed",
                "released_at": fields.Datetime.now(),
                "note": reason or line.note,
            })
            line.card_id.message_post(body=_(
                "Draw of %(amount)s FAILED and was not charged. %(reason)s",
                amount=line.draw_amount, reason=reason or "",
            ))
            line.card_id._sync_depleted()
        return True

    def unlink(self):
        # The ledger is append-only: a deleted line would change a balance the
        # customer was already shown and break reconstruction from history.
        raise UserError(_(
            "Gift card draws cannot be deleted. Release or fail the draw "
            "instead — both leave the history intact."))
