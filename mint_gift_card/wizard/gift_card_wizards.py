# -*- coding: utf-8 -*-
"""Manual controls for exercising the ledger by hand.

Phase 1 ships the ledger without the draw engine, so `hold` / `settle` /
`release` have no caller yet. These wizards are that caller — they let a human
walk a card through a real partial redemption in the Odoo UI and watch the
balance behave, before any of it is wired to a register.

They are deliberately NOT the draw path. A real draw reads the live basket,
mints a single-use Dutchie coupon for the exact amount and applies it; none of
that happens here. Nothing in these wizards touches Dutchie, so a draw recorded
this way discounts nothing at a register — it only moves the ledger.

Manager-only, and labelled as manual throughout, because these buttons move
money on an instrument a customer is holding.
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class MintGiftCardDrawWizard(models.TransientModel):
    _name = "mint.gift.card.draw.wizard"
    _description = "Record a Manual Gift Card Draw"

    card_id = fields.Many2one(
        "mint.gift.card", string="Gift Card", required=True, readonly=True,
    )
    currency_id = fields.Many2one(related="card_id.currency_id", readonly=True)
    balance = fields.Monetary(
        related="card_id.balance", string="Available", readonly=True,
        currency_field="currency_id",
    )
    amount = fields.Monetary(
        string="Draw Amount", required=True, currency_field="currency_id",
        help="What to take from the card. A real draw would use "
             "min(balance, basket total); here you choose, so you can test "
             "what happens at and beyond the limit.",
    )
    shipment_id = fields.Char(
        string="Shipment ID",
        help="Optional. Free text in this wizard — a real draw resolves it "
             "from the customer's live checked-in transaction.",
    )
    child_code = fields.Char(
        string="Child Coupon Code",
        help="Optional. A real draw mints this in Dutchie; typing one here "
             "records the reference without creating anything.",
    )

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        card = self.env["mint.gift.card"].browse(self.env.context.get("active_id"))
        if card.exists():
            res["card_id"] = card.id
            # Default to the whole remaining balance — the common test is
            # "spend some, confirm the rest survives", and this makes the
            # limit case one click away.
            res.setdefault("amount", card.max_drawable())
        return res

    def action_confirm(self):
        self.ensure_one()
        line = self.card_id.hold(
            self.amount,
            shipment_id=self.shipment_id or None,
            child_code=self.child_code or None,
        )
        return {
            "type": "ir.actions.act_window",
            "res_model": "mint.gift.card",
            "res_id": self.card_id.id,
            "view_mode": "form",
            "target": "current",
            "context": {"draw_line_id": line.id},
        }


class MintGiftCardSettleWizard(models.TransientModel):
    _name = "mint.gift.card.settle.wizard"
    _description = "Settle a Gift Card Draw"

    line_id = fields.Many2one(
        "mint.gift.card.line", string="Draw", required=True, readonly=True,
    )
    currency_id = fields.Many2one(related="line_id.currency_id", readonly=True)
    draw_amount = fields.Monetary(
        related="line_id.draw_amount", string="Drawn", readonly=True,
        currency_field="currency_id",
    )
    amount = fields.Monetary(
        string="Settled Amount", required=True, currency_field="currency_id",
        help="What Dutchie's report shows was actually discounted. Settling "
             "less than was drawn returns the difference to the card — the "
             "normal outcome when a basket shrinks after the coupon goes on.",
    )
    order_id = fields.Char(
        string="Dutchie Order ID",
        help="Optional. In production this arrives from report 10875.",
    )

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        line = self.env["mint.gift.card.line"].browse(self.env.context.get("active_id"))
        if line.exists():
            res["line_id"] = line.id
            res.setdefault("amount", line.draw_amount)
        return res

    def action_confirm(self):
        self.ensure_one()
        if self.line_id.state != "held":
            raise UserError(_("Only a held draw can be settled."))
        self.line_id.action_settle(self.amount, order_id=self.order_id or None)
        return {"type": "ir.actions.act_window_close"}
