# -*- coding: utf-8 -*-
"""Customer opt-in for spending Mint Bucks without being asked."""
from odoo import fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    x_gift_card_auto_use = fields.Boolean(
        string="Auto-use Mint Bucks",
        default=False,
        copy=False,
        index=True,
        help=(
            "When set, a Mint Bucks balance is drawn against this customer's "
            "in-store transaction automatically at check-in, without them "
            "opening the app.\n\n"
            "Off by default, and deliberately opt-in rather than a setting we "
            "choose for people. A coupon is use-it-or-lose-it, so applying one "
            "unasked only ever helps. Store credit is different: it does not "
            "expire, so spending it is a decision about WHEN, and somebody "
            "holding a balance for a particular purchase would find it spent "
            "across routine trips instead. The existing two-tap flow — plan "
            "the draw, then confirm it — is where that choice is made today; "
            "this flag is the customer saying they would rather not be asked "
            "each time.\n\n"
            "The draw amount is unchanged either way: min(balance, what the "
            "basket still owes), so a draw never overshoots and the remainder "
            "stays on the card."
        ),
    )
