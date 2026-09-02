# -*- coding: utf-8 -*-
"""The draw engine — the half that talks to the outside world.

Kept in its own file because the ledger deliberately does not know Dutchie
exists. `gift_card.py` only moves money between held / settled / released; this
file is what reads a live basket, decides how much to take, and (in the next
chunk) mints and applies the one-shot coupon that actually delivers it.

The sequence a draw follows:

    1. read     the customer's live basket from the register
    2. compute  draw = min(card balance, what the basket still owes)
    3. mint     a single-use Dutchie coupon for exactly that amount
    4. apply    it to their shipment
    5. hold     the amount on the ledger, pending settlement

This chunk implements 1 and 2, plus a dry run that stops before any write. That
ordering is on purpose: a draw that computes the wrong number is a customer
being over- or under-charged, and it is far cheaper to find that out against a
real basket with nothing minted than to discover it after a coupon is live.
"""
import json
import logging
import urllib.error
import urllib.request

from odoo import _, api, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Which basket total a gift card draws against.
#
# 🚨 UNSETTLED. `readCart` returns both `grandTotal` and `subTotal`, and nobody
# has yet confirmed on a real basket — one that ALREADY carries a discount —
# which of them represents what the customer still owes. Getting this wrong
# mis-spends the card on every single transaction, so it is a switch with a
# stated default rather than a silent assumption buried in an expression.
#
# Default `grand_total`: a gift card is tender-like, and a customer handing over
# a $50 card expects it against the number on the pin pad, tax included.
# Flip to `sub_total` if the live test says otherwise.
DRAW_BASIS_PARAM = "mint_gift_card.draw_basis"
DRAW_BASIS_DEFAULT = "grand_total"

INVSVC_TIMEOUT = 60


class MintGiftCardDraw(models.Model):
    _inherit = "mint.gift.card"

    # ── invsvc plumbing ─────────────────────────────────────────────────
    @api.model
    def _invsvc(self):
        """Base URL and key for the inventory service.

        Reuses the Dutchie push configuration rather than adding a second set
        of parameters — one service, one credential, and no chance of the two
        drifting to different hosts. The push URL is stored as the full
        `/api/admin/discounts` endpoint, so the base is derived the same way
        the coupon-usage sweep already derives it.
        """
        push = self.env["mint.ptl.day"].sudo()
        base = (push._get_dutchie_push_url() or "").rsplit("/api/admin/discounts", 1)[0]
        key = push._get_dutchie_push_api_key()
        if not base or not key:
            raise UserError(_(
                "The inventory service URL or API key is not configured — set "
                "mint.dutchie_discount_push.url and .api_key."))
        return base, key

    @api.model
    def _invsvc_post(self, path, payload, timeout=INVSVC_TIMEOUT):
        """POST JSON to invsvc and return (status, body).

        Errors are returned rather than raised: every caller here has to tell a
        refusal (the coupon was declined, the customer is not checked in) apart
        from a fault (the service is down), and those need different handling —
        one is the customer's situation, the other is ours.
        """
        base, key = self._invsvc()
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            "%s%s" % (base, path), data=data, method="POST",
            headers={
                "Content-Type": "application/json",
                "X-API-Key": key,
                "User-Agent": "mint-odoo-gift-card-draw/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(raw or "{}")
            except ValueError:
                return e.code, {"error": raw[:300]}
        except Exception as e:
            _logger.warning("gift_card draw: invsvc %s failed: %s", path, e)
            return 0, {"error": str(e)}

    # ── Step 1: read the live basket ────────────────────────────────────
    @api.model
    def read_basket(self, loc_id, lsp_id, shipment_id, customer_id=None, register=None):
        """Read what is actually in front of the customer.

        Returns None when the basket cannot be read, which is NOT the same as
        an empty one — invsvc reports those distinctly and so must we. Drawing
        against an unreadable basket would mean guessing at the amount, so the
        engine refuses instead.
        """
        status, body = self._invsvc_post("/api/customer/cart-facts", {
            "shipmentId": shipment_id,
            "locId": loc_id,
            "lspId": lsp_id,
            "customerId": customer_id or 0,
            "register": register or 0,
        })
        if status != 200 or not body.get("ok"):
            _logger.warning("gift_card draw: cart-facts %s -> %s %s",
                            shipment_id, status, str(body)[:200])
            return None
        if not body.get("readable"):
            return None
        return {
            "items": body.get("items") or [],
            "total_items": body.get("totalItems") or 0,
            "grand_total": float(body.get("grandTotal") or 0.0),
            "sub_total": float(body.get("subTotal") or 0.0),
            "applied_discount_ids": body.get("appliedDiscountIds") or [],
        }

    # ── Step 2: decide the amount ───────────────────────────────────────
    @api.model
    def _draw_basis(self):
        return (self.env["ir.config_parameter"].sudo()
                .get_param(DRAW_BASIS_PARAM, DRAW_BASIS_DEFAULT) or DRAW_BASIS_DEFAULT)

    @api.model
    def basket_payable(self, cart):
        """What the basket still owes, per the configured basis."""
        basis = self._draw_basis()
        value = cart["sub_total"] if basis == "sub_total" else cart["grand_total"]
        return max(0.0, float(value or 0.0))

    def compute_draw(self, cart):
        """min(what the card holds, what the basket owes).

        Never more than the customer is spending — a gift card gives no change,
        and a coupon minted above the basket total would destroy the difference,
        which is the exact behaviour this whole design exists to avoid.
        """
        self.ensure_one()
        payable = self.basket_payable(cart)
        drawable = self.max_drawable()
        return self.currency_id.round(min(drawable, payable))

    # ── Dry run ─────────────────────────────────────────────────────────
    def plan_draw(self, loc_id, lsp_id, shipment_id, customer_id=None, register=None):
        """Resolve everything a real draw needs and stop before any write.

        This is the safe way to exercise matching and arithmetic against a live
        register. It mints nothing and applies nothing, so it can be run
        against production while the numbers are still being trusted.
        """
        self.ensure_one()
        result = {
            "ok": False,
            "card": self.code,
            "balance": self.balance,
            "spendable": self.is_spendable,
            "basis": self._draw_basis(),
            "shipment_id": shipment_id,
        }

        if not self.is_spendable:
            result["error"] = "card_not_spendable"
            result["message"] = _(
                "Card %(code)s is %(state)s with %(bal)s remaining.",
                code=self.code, state=self.state, bal=self.balance)
            return result

        if self.store_ids:
            push = self.env["mint.ptl.day"].sudo()
            allowed = {push._resolve_pos_loc_id(s) for s in self.store_ids}
            if loc_id not in allowed:
                # store_ids gates OUR draw path. A minted child is LSP-wide at
                # the register regardless, so this is the only place the scope
                # is actually enforced.
                result["error"] = "store_not_allowed"
                result["message"] = _("This card cannot be used at that store.")
                return result

        cart = self.read_basket(loc_id, lsp_id, shipment_id, customer_id, register)
        if cart is None:
            result["error"] = "cart_unreadable"
            result["message"] = _(
                "Could not read that basket. Refusing to draw rather than "
                "guess at the amount.")
            return result

        payable = self.basket_payable(cart)
        if payable <= 0:
            result["error"] = "nothing_to_pay"
            result["message"] = _("That basket has nothing left to pay.")
            result["cart"] = cart
            return result

        amount = self.compute_draw(cart)
        result.update({
            "ok": True,
            "amount": amount,
            "payable": payable,
            "grand_total": cart["grand_total"],
            "sub_total": cart["sub_total"],
            "total_items": cart["total_items"],
            "balance_after": self.currency_id.round(self.balance - amount),
            "covers_basket": self.currency_id.compare_amounts(amount, payable) >= 0,
        })
        return result
