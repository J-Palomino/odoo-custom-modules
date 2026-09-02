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
import secrets
import urllib.error
import urllib.request

from odoo import _, api, fields, models
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

    # ── Step 3: mint the one-shot child coupon ──────────────────────────
    #
    # Shape copied field-for-field from the two coupons already proven to work
    # at a register (mint.discount 3337/3340 -> Dutchie 385839/385840). Do not
    # "tidy" these values:
    #   * discount_type 'dollar_off_total' is contributed by
    #     mint_dutchie_discount_mirror, not by the base selection.
    #   * threshold_type 'order_total' + threshold_min 0.01 is load-bearing:
    #     a 'none' threshold makes invsvc refuse the write with 422
    #     unscoped_single_item_coupon.
    #   * item_group_type_id is left at 0 so the payload builder applies its
    #     own fallback of 5, which is what the live records emit.
    #   * maximum_usage_count MUST be >= 1. Zero means UNCAPPED in Dutchie.
    CHILD_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
    CHILD_CODE_PREFIX = "MINT-GD-"

    @api.model
    def _generate_child_code(self):
        Discount = self.env["mint.discount"].sudo()
        for _attempt in range(12):
            body = "".join(secrets.choice(self.CHILD_CODE_ALPHABET) for _ in range(6))
            code = "%s%s" % (self.CHILD_CODE_PREFIX, body)
            if not Discount.search_count([("dutchie_discount_code", "=", code)]):
                return code
        raise UserError(_("Could not generate a unique draw code."))

    @api.model
    def _store_for_loc(self, loc_id):
        push = self.env["mint.ptl.day"].sudo()
        for company in self.env["res.company"].sudo().search(
                [("dutchie_pos_location_id", "!=", False)]):
            if push._resolve_pos_loc_id(company) == loc_id:
                return company
        return self.env["res.company"].browse()

    def _mint_child(self, amount, loc_id):
        """Create and publish a single-use Dutchie coupon for exactly `amount`.

        Returns (discount, dutchie_id). Raises if the coupon did not reach
        Dutchie — a child that exists only in Odoo would let us hold money
        against a coupon the register has never heard of.
        """
        self.ensure_one()
        store = self._store_for_loc(loc_id)
        if not store:
            raise UserError(_("No store is mapped to Dutchie LocId %s.", loc_id))

        today = fields.Date.context_today(self)
        code = self._generate_child_code()
        child = self.env["mint.discount"].sudo().create({
            "name": "Gift card draw %s — %s" % (self.code, code),
            "description": "lgm | gift card %s draw of %s" % (self.code, amount),
            "discount_type": "dollar_off_total",
            "calculation_method_id": 5,
            "discount_value": amount,
            "discount_amount": amount,
            "application_method": "code",
            "code": code,
            "dutchie_discount_code": code,
            "threshold_type": "order_total",
            "threshold_min": 0.01,
            "maximum_usage_count": 1,
            "store_ids": [(6, 0, store.ids)],
            "valid_from": today,
            "valid_until": today,
            "is_published": True,
            "source": "manual",
            "monday": True, "tuesday": True, "wednesday": True, "thursday": True,
            "friday": True, "saturday": True, "sunday": True,
        })

        child.action_publish_to_dutchie()

        # action_publish_to_dutchie reports success through the push LOG, not
        # by writing back onto the discount — mint.discount.dutchie_discount_id
        # is False even on the coupons that demonstrably work at a register.
        log = self.env["mint.dutchie.discount.push.log"].sudo().search(
            [("discount_id", "=", child.id)], order="id desc", limit=1)
        if not log or not log.success or not log.dutchie_discount_id:
            raise UserError(_(
                "The draw coupon did not reach Dutchie (%(err)s). Nothing was "
                "charged to the card.",
                err=(log.error_message or "no push log")[:200] if log else "no push log"))
        return child, log.dutchie_discount_id

    def _retire_child(self, child):
        """Delete a child coupon in Dutchie. Best-effort, never raises.

        Called both when an apply is refused and after settlement. Leaving
        spent children behind is not cosmetic: one store already carries 521
        discounts, that list is pulled on every sync, and retired discounts do
        not expire on their own.
        """
        try:
            push = self.env["mint.ptl.day"].sudo()
            mode = push._get_dutchie_push_mode()
            url = push._get_dutchie_push_url()
            api_key = push._get_dutchie_push_api_key()
            Log = self.env["mint.dutchie.discount.push.log"].sudo()
            for store in push._collapse_stores_by_lsp(child.store_ids):
                push._push_one_discount(child, store, mode, url, api_key, Log, is_delete=True)
            child.sudo().write({"is_published": False})
            return True
        except Exception as e:
            # A child left alive is a cleanup problem, not a money problem: it
            # is single-use and expires today. Log loudly, never propagate.
            _logger.warning("gift_card draw: could not retire child %s: %s",
                            child.dutchie_discount_code, e)
            return False

    # ── Step 4: apply it to the basket ──────────────────────────────────
    def _apply_child(self, code, loc_id, lsp_id, shipment_id, customer_id=None):
        """Put the child coupon on the customer's live transaction.

        Returns (applied, message). A 409 is Dutchie REFUSING the coupon —
        expired, already redeemed, wrong store — which is the customer's
        situation, not a fault. Anything else is ours.
        """
        status, body = self._invsvc_post("/api/customer/apply-coupon", {
            "code": code,
            "locId": loc_id,
            "lspId": lsp_id,
            "shipmentId": shipment_id,
            "customerId": customer_id or 0,
        })
        if status == 200 and body.get("ok"):
            return True, body.get("message")
        return False, (body.get("message") or body.get("error")
                       or "invsvc HTTP %s" % status)

    # ── The whole draw ──────────────────────────────────────────────────
    def execute_draw(self, loc_id, lsp_id, shipment_id, customer_id=None,
                     register=None, idempotency_key=None):
        """Spend part of this card against a live basket.

        Order is deliberate: the HOLD comes before the mint. The hold is the
        serialized reservation, so taking it first means the money is committed
        before anything exists in Dutchie. Minting first and failing to hold
        would leave a live coupon with no ledger backing it — free money.

        Fails closed throughout: any failure after the mint retires the child
        coupon and frees the hold, so a half-finished draw never leaves a
        spendable coupon behind.
        """
        self.ensure_one()
        plan = self.plan_draw(loc_id, lsp_id, shipment_id, customer_id, register)
        if not plan.get("ok"):
            return plan

        amount = plan["amount"]

        # 1. Reserve the money. Re-checks the balance under a row lock, so a
        #    concurrent draw between plan and here is refused rather than
        #    overdrawing.
        try:
            line = self.hold(
                amount, shipment_id=shipment_id, loc_id=loc_id, lsp_id=lsp_id,
                idempotency_key=idempotency_key,
            )
        except UserError as e:
            return dict(plan, ok=False, error="hold_refused", message=str(e))

        if line.state != "held" or line.child_code:
            # Idempotency handed back an earlier attempt. Two shapes of that:
            # a line that already reached a terminal state, and one still held
            # that had already minted its coupon. Both must return the original
            # draw — re-minting on a held line would put a SECOND live coupon
            # into Dutchie for money the ledger has reserved only once.
            return dict(plan, ok=True, replayed=True, line_id=line.id,
                        state=line.state, amount=line.draw_amount,
                        child_code=line.child_code)

        # 2. Mint the one-shot coupon for exactly this amount.
        try:
            child, dutchie_id = self._mint_child(amount, loc_id)
        except Exception as e:
            line.action_fail(reason=_("Could not mint the draw coupon: %s", e))
            return dict(plan, ok=False, error="mint_failed", message=str(e))

        line.write({
            "child_discount_id": child.id,
            "child_code": child.dutchie_discount_code,
            "dutchie_discount_id": str(dutchie_id),
        })

        # 3. Apply it to their transaction.
        applied, message = self._apply_child(
            child.dutchie_discount_code, loc_id, lsp_id, shipment_id, customer_id)
        if not applied:
            self._retire_child(child)
            line.action_fail(reason=_("Register refused the coupon: %s", message))
            return dict(plan, ok=False, error="apply_refused", message=message,
                        child_code=child.dutchie_discount_code)

        _logger.info("gift_card draw: %s drew %s on shipment %s via %s",
                     self.code, amount, shipment_id, child.dutchie_discount_code)
        return dict(plan, ok=True, line_id=line.id, amount=amount,
                    child_code=child.dutchie_discount_code,
                    dutchie_discount_id=dutchie_id,
                    balance_after=self.balance, message=message)
