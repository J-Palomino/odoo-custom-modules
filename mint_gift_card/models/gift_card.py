# -*- coding: utf-8 -*-
"""Stored-value gift cards — the ledger Dutchie cannot provide.

A Dutchie discount is all-or-nothing: a $100 code spent against a $30 basket
destroys the other $70, and a code with MaxRedemptions=10 grants the FULL face
value ten times over (measured live: `MINT-GMXT6F`, issued as "$100 off", has
paid out $300 across four partial draws). Nothing in `mint.discount` tracks
dollars remaining, so a remainder cannot be represented there.

This model is the balance. The customer-facing `code` here is **never pushed to
Dutchie** — it is spendable only through our own draw path, which mints a
separate single-use coupon for the exact amount of one draw. That is what makes
a leaked card code worthless at a register, and it caps the loss from a leaked
CHILD code at that one draw.

Money moves in two phases, never one:

    hold(amount)      provisional debit taken the moment a draw is applied
    settle(line, $)   confirmed from Dutchie report 10875, which carries both
                      `Discount Code` and `Discounted Amount`
    release(line)     the hold returns to the card — an abandoned basket must
                      not silently cost the customer their balance

Settling optimistically would overcharge on a basket edited after apply;
settling lazily would let a card be drawn twice. Two phases is the only shape
that is correct in both directions.
"""
import logging
import secrets

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

# Unambiguous alphabet — no 0/O, 1/I/L, U/V confusion. Matches the alphabet the
# promos coupon issuer already uses, so staff read codes off a screen the same
# way regardless of which instrument they are holding.
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 8
CODE_PREFIX = "MINT-GC-"

# How long a hold may sit unconfirmed before the sweeper returns it to the card.
# Report 10875 is polled hourly, so this must comfortably exceed one poll cycle
# or a live redemption would be released out from under itself.
STALE_HOLD_HOURS_PARAM = "mint_gift_card.stale_hold_hours"
STALE_HOLD_HOURS_DEFAULT = 6


class MintGiftCard(models.Model):
    _name = "mint.gift.card"
    _description = "Stored-Value Gift Card"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "create_date desc, id desc"
    _rec_name = "code"

    code = fields.Char(
        string="Card Code", required=True, index=True, copy=False, tracking=True,
        default=lambda self: self._generate_code(),
        help="Customer-facing code. NEVER pushed to Dutchie — it is redeemable "
             "only through the Mint draw path, which mints a separate one-shot "
             "coupon per draw. A stolen card code is worthless at a register.",
    )
    partner_id = fields.Many2one(
        "res.partner", string="Holder", index=True, tracking=True,
        help="Optional. A card with no holder is a bearer instrument — the "
             "secrecy of the code is then the only gate, exactly as with a "
             "physical gift card.",
    )
    issued_by_id = fields.Many2one(
        "res.partner", string="Issued By", index=True, tracking=True,
        help="Who minted this card. Mirrors mint.discount.promo_issued_by_id "
             "so promos and gift cards audit the same way.",
    )

    # ── Accounting scope ────────────────────────────────────────────────
    # There is deliberately NO "sold for cash" option. A purchased gift card is
    # deferred revenue (a liability), but every draw here books as a Dutchie
    # DISCOUNT — the GL would see a markdown and never the liability. Until
    # either the GeneriPay tender rail is contracted or accounting signs off on
    # a manual journal, this instrument is promotional credit only, and the
    # schema is what enforces that rather than a note someone can miss.
    issue_reason = fields.Selection(
        [
            ("service_recovery", "Service Recovery"),
            ("promotion", "Promotion / Marketing"),
            ("employee", "Employee Appreciation"),
            ("contest", "Contest or Prize"),
            ("other", "Other (explain in notes)"),
        ],
        string="Reason Issued", required=True, default="service_recovery", tracking=True,
        help="Promotional and comp credit only — all of these are marketing "
             "expense, which is what makes a discount the accounting-correct "
             "shape. Selling a card for money needs a payment tender, not this.",
    )

    currency_id = fields.Many2one(
        "res.currency", string="Currency", required=True,
        default=lambda self: self.env.company.currency_id,
    )
    face_value = fields.Monetary(
        string="Face Value", required=True, currency_field="currency_id", tracking=True,
        help="Amount loaded at issue. Immutable once the card leaves draft — "
             "topping up a live card would silently rewrite history the "
             "settlement sweep reconciles against.",
    )
    held_amount = fields.Monetary(
        string="On Hold", compute="_compute_amounts", store=True,
        currency_field="currency_id",
        help="Draws applied to a basket but not yet confirmed in report 10875.",
    )
    settled_amount = fields.Monetary(
        string="Settled", compute="_compute_amounts", store=True,
        currency_field="currency_id",
        help="Dollars confirmed spent, summed from Dutchie's own report.",
    )
    balance = fields.Monetary(
        string="Balance", compute="_compute_amounts", store=True,
        currency_field="currency_id", tracking=True,
        help="face_value − settled − held. Computed from the lines, never "
             "written: the lines are the truth and this is a view of them.",
    )

    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("active", "Active"),
            ("depleted", "Depleted"),
            ("expired", "Expired"),
            ("void", "Void"),
        ],
        string="Status", default="draft", required=True, index=True, tracking=True,
        help="Human intent, not a derived value — 'void' in particular is a "
             "decision. Spendability folds this with balance and expiry; see "
             "is_spendable.",
    )
    is_spendable = fields.Boolean(
        string="Spendable Now", compute="_compute_is_spendable", store=True, index=True,
        help="TRUE iff active, in date, and carrying a positive balance. This "
             "is what the draw path checks — never `state` alone.",
    )

    expires_at = fields.Date(
        string="Expires", tracking=True,
        help="Optional. Promotional credit may expire; a card someone PAID for "
             "is constrained by state and federal rules, which is one more "
             "reason this instrument is comp-only for now.",
    )
    store_ids = fields.Many2many(
        "res.company", string="Redeemable At",
        help="Stores where this card may be drawn against. Empty means every "
             "store. Note a minted child coupon is LSP-wide at the register "
             "regardless, so this gates OUR draw path, not Dutchie's.",
    )
    company_id = fields.Many2one(
        "res.company", string="Owning Company", index=True,
        help="Left empty by default so a card is visible across the group — a "
             "bearer instrument redeemable LSP-wide should not be hidden from "
             "the store that has to honour it.",
    )

    line_ids = fields.One2many("mint.gift.card.line", "card_id", string="Draws")
    line_count = fields.Integer(string="Draws", compute="_compute_line_count")
    note = fields.Text(string="Notes")

    _code_uniq = models.Constraint(
        "UNIQUE(code)",
        "That gift card code already exists.",
    )
    _face_value_positive = models.Constraint(
        "CHECK(face_value > 0)",
        "A gift card must be issued with a positive face value.",
    )

    # ── Code generation ─────────────────────────────────────────────────
    @api.model
    def _generate_code(self):
        """Mint an unused card code.

        Retries on collision rather than trusting entropy blindly: the UNIQUE
        constraint would otherwise surface a random 1-in-a-few-billion failure
        to whoever happened to be issuing a card at the time.
        """
        for _attempt in range(12):
            body = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
            code = f"{CODE_PREFIX}{body}"
            if not self.sudo().search_count([("code", "=", code)]):
                return code
        raise UserError(_("Could not generate a unique gift card code. Try again."))

    # ── Computes ────────────────────────────────────────────────────────
    @api.depends("face_value", "currency_id",
                 "line_ids.state", "line_ids.draw_amount", "line_ids.settled_amount")
    def _compute_amounts(self):
        for card in self:
            held = sum(
                line.draw_amount for line in card.line_ids if line.state == "held"
            )
            settled = sum(
                line.settled_amount for line in card.line_ids if line.state == "settled"
            )
            rounding = card.currency_id.round if card.currency_id else (lambda v: v)
            card.held_amount = rounding(held)
            card.settled_amount = rounding(settled)
            card.balance = rounding((card.face_value or 0.0) - settled - held)

    @api.depends("state", "balance", "expires_at")
    def _compute_is_spendable(self):
        today = fields.Date.context_today(self)
        for card in self:
            in_date = not card.expires_at or card.expires_at >= today
            card.is_spendable = bool(
                card.state == "active"
                and in_date
                and card.currency_id.compare_amounts(card.balance, 0.0) > 0
            )

    @api.depends("line_ids")
    def _compute_line_count(self):
        for card in self:
            card.line_count = len(card.line_ids)

    # ── Guards ──────────────────────────────────────────────────────────
    @api.constrains("face_value", "state")
    def _check_face_value_immutable_when_live(self):
        for card in self:
            if card.state in ("active", "depleted") and card.settled_amount \
                    and card.currency_id.compare_amounts(card.face_value, card.settled_amount) < 0:
                raise ValidationError(_(
                    "Face value (%(face)s) cannot be lowered below what has "
                    "already been spent (%(spent)s) on card %(code)s.",
                    face=card.face_value, spent=card.settled_amount, code=card.code,
                ))

    def write(self, vals):
        # Face value is the anchor the settlement sweep reconciles against.
        # Editing it on a live card would retroactively change every balance
        # ever shown to the customer, so it is refused outright rather than
        # tracked — a correction is a new card plus a void, which is auditable.
        if "face_value" in vals:
            live = self.filtered(lambda c: c.state not in ("draft",))
            if live:
                raise UserError(_(
                    "Face value can only be set while a card is in draft. "
                    "Void card %s and issue a replacement instead.",
                    ", ".join(live.mapped("code")),
                ))
        return super().write(vals)

    # ── State transitions ───────────────────────────────────────────────
    def action_activate(self):
        for card in self:
            if card.state != "draft":
                raise UserError(_("Only a draft card can be activated (%s).", card.code))
            card.state = "active"
            card.message_post(body=_(
                "Activated with a balance of %(amount)s %(currency)s.",
                amount=card.face_value, currency=card.currency_id.name,
            ))
        return True

    def action_void(self):
        """Cancel a card and return every outstanding hold.

        Releasing first matters: a void that left holds standing would leave
        lines claiming money against a dead card, and the settlement sweep
        would keep looking for them.
        """
        for card in self:
            if card.state == "void":
                continue
            outstanding = card.line_ids.filtered(lambda line: line.state == "held")
            for line in outstanding:
                line.action_release(reason=_("Card voided"))
            card.state = "void"
            card.message_post(body=_(
                "Voided. %(n)s outstanding hold(s) released.", n=len(outstanding),
            ))
        return True

    # ── Ledger API — the surface the draw engine (Phase 2) calls ────────
    def max_drawable(self):
        """What this card could pay toward a basket right now.

        Deliberately separate from `hold`: deciding how much to draw is the
        draw engine's job (it also knows the basket total), while refusing an
        overdraw is the ledger's. Keeping them apart is what lets the ledger
        stay honest about a caller that asks for too much.
        """
        self.ensure_one()
        if not self.is_spendable:
            return 0.0
        return max(0.0, self.balance)

    def hold(self, amount, shipment_id=None, loc_id=None, lsp_id=None,
             child_code=None, child_discount_id=None, dutchie_discount_id=None,
             idempotency_key=None):
        """Take a provisional debit and return its ledger line.

        Locks the card row for the duration. Two registers drawing on one card
        must serialize, or both read the same balance and both succeed — which
        is how a $50 card pays out $100.
        """
        self.ensure_one()
        currency = self.currency_id

        if currency.compare_amounts(amount, 0.0) <= 0:
            raise UserError(_("A draw must be for a positive amount."))

        # Serialize concurrent draws on this card. Taken before ANY read that
        # the decision depends on — including the idempotency lookup, because
        # two simultaneous retries of the same request would otherwise both
        # miss the existing line and race to insert, turning a case this method
        # is meant to absorb into a constraint violation.
        self.env.cr.execute(
            "SELECT id FROM mint_gift_card WHERE id = %s FOR UPDATE", (self.id,)
        )

        if idempotency_key:
            existing = self.env["mint.gift.card.line"].search([
                ("card_id", "=", self.id),
                ("idempotency_key", "=", idempotency_key),
            ], limit=1)
            if existing:
                # A retry or a double-tap. Returning the original line — rather
                # than drawing again — is the entire point of the key.
                return existing
        # is_spendable is a STORED compute derived from balance, so invalidating
        # the balance alone would leave the guard below reading a stale value
        # written before the lock was taken — exactly the window this lock
        # exists to close.
        self.invalidate_recordset(
            ["balance", "held_amount", "settled_amount", "is_spendable", "state"]
        )

        if not self.is_spendable:
            raise UserError(_(
                "Card %(code)s is not spendable (status %(state)s, balance %(bal)s).",
                code=self.code, state=self.state, bal=self.balance,
            ))
        if currency.compare_amounts(amount, self.balance) > 0:
            raise UserError(_(
                "Draw of %(want)s exceeds the %(bal)s remaining on card %(code)s.",
                want=amount, bal=self.balance, code=self.code,
            ))

        line = self.env["mint.gift.card.line"].create({
            "card_id": self.id,
            "state": "held",
            "draw_amount": currency.round(amount),
            "shipment_id": shipment_id,
            "loc_id": loc_id,
            "lsp_id": lsp_id,
            "child_code": child_code,
            "child_discount_id": child_discount_id,
            "dutchie_discount_id": dutchie_discount_id,
            "idempotency_key": idempotency_key,
            "held_at": fields.Datetime.now(),
        })
        self.message_post(body=_(
            "Held %(amount)s for shipment %(ship)s (child code %(child)s).",
            amount=amount, ship=shipment_id or "—", child=child_code or "—",
        ))
        self._sync_depleted()
        return line

    def _sync_depleted(self):
        """Flip to depleted once nothing is left, and back if a hold returns."""
        for card in self:
            zero = card.currency_id.compare_amounts(card.balance, 0.0) <= 0
            if zero and card.state == "active":
                card.state = "depleted"
            elif not zero and card.state == "depleted":
                card.state = "active"

    # ── Crons ───────────────────────────────────────────────────────────
    @api.model
    def _cron_expire_and_release(self):
        """Expire dated-out cards and return holds nothing ever confirmed.

        The release half is the one that protects customers: without it, a
        basket abandoned after a draw was applied would quietly keep the money.
        """
        today = fields.Date.context_today(self)
        stale_hours = int(self.env["ir.config_parameter"].sudo().get_param(
            STALE_HOLD_HOURS_PARAM, STALE_HOLD_HOURS_DEFAULT))
        cutoff = fields.Datetime.subtract(fields.Datetime.now(), hours=stale_hours)

        stale = self.env["mint.gift.card.line"].sudo().search([
            ("state", "=", "held"),
            ("held_at", "<", cutoff),
        ])
        for line in stale:
            line.action_release(reason=_(
                "No redemption confirmed within %s hours", stale_hours))

        expiring = self.sudo().search([
            ("state", "in", ["active", "depleted"]),
            ("expires_at", "!=", False),
            ("expires_at", "<", today),
        ])
        if expiring:
            expiring.write({"state": "expired"})

        _logger.info(
            "gift_card cron: released %d stale hold(s), expired %d card(s)",
            len(stale), len(expiring),
        )
        return len(stale) + len(expiring)
