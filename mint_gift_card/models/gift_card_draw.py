# -*- coding: utf-8 -*-
"""The draw engine — the half that talks to the outside world.

Kept in its own file because the ledger deliberately does not know Dutchie
exists. `gift_card.py` only moves money between held / settled / released; this
file is everything that reaches out to a register.

The sequence a draw follows:

    1. read     the customer's live basket from the register
    2. compute  draw = min(card balance, what the basket still owes)
    3. mint     a single-use Dutchie coupon for exactly that amount
    4. apply    it to their shipment
    5. hold     the amount on the ledger, pending settlement

    6. settle   confirm the dollars from Dutchie report 10875, then retire
                the spent coupon

Two ordering decisions carry the safety of the whole thing. The HOLD is taken
before anything reaches Dutchie, so money is reserved before a coupon exists;
minting first and failing to hold would leave a live coupon with no ledger
entry behind it. And settlement CONFIRMS rather than assumes — the basket can
change after a coupon goes on, so what the register actually took is read back
from Dutchie's own report rather than presumed equal to what we drew.

`plan_draw` is a dry run of steps 1-2 that writes nothing, and is safe to run
against production while the numbers are still being trusted.
"""
import json
import logging
import secrets
import urllib.error
import urllib.parse
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

    # ── Step 5: settlement ──────────────────────────────────────────────
    #
    # Report 10875 ("Discount Detail Report") is the only Dutchie source that
    # names WHICH discount a transaction used AND how many dollars it took.
    # Verified live 2026-09-01 — its columns include both `Discount Code` and
    # `Discounted Amount`. Dutchie's own RedemptionCount is unusable: it still
    # reads 0 after redemptions confirmed at a register.
    #
    # Because every draw mints its OWN single-use code, attribution here is
    # exact — one code, one order, no disentangling several redemptions that
    # share an identifier.
    USAGE_REPORT_ID = 10875
    USAGE_NEEDLE = "MINT-GD-"
    _USAGE_CODE_KEYS = ("discountcode",)
    _USAGE_ORDER_KEYS = ("orderid",)
    _USAGE_AMOUNT_KEYS = ("discountedamount",)

    @api.model
    def _invsvc_get(self, path, timeout=240):
        base, key = self._invsvc()
        req = urllib.request.Request(
            "%s%s" % (base, path),
            headers={"X-API-Key": key,
                     "User-Agent": "mint-odoo-gift-card-settle/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8", "replace") or "{}")
        except Exception as e:
            _logger.warning("gift_card settle: invsvc GET %s failed: %s", path, e)
            return 0, {"error": str(e)}

    @staticmethod
    def _row_get(row, candidates):
        """Read a report column by any of `candidates`, ignoring case, spaces
        and underscores. Dutchie columns are human-facing labels ("Order ID",
        "Discount Code"), so matching one exact key is a coin flip — and a
        miss reads identically to "never redeemed", which is the exact silent
        failure this whole settlement path exists to avoid."""
        norm = {str(k).lower().replace(" ", "").replace("_", ""): v
                for k, v in (row or {}).items()}
        for cand in candidates:
            v = norm.get(cand.replace("_", ""))
            if v not in (None, ""):
                return v
        return None

    @api.model
    def _settlement_targets(self, lines):
        """Every (loc, lsp) a held draw could have been redeemed at.

        🚨 A Dutchie code is LSP-WIDE while report 10875 is LOCATION-scoped.
        Measured on the coupon path: MINT-GMXT6F was pushed to 75th Ave (2679)
        and redeemed at Tempe (1568) and Northern (2272) — querying only the
        store it was pushed to returns zero rows, silently. So sweep every
        location in the LSP, not the one we applied at.
        """
        push = self.env["mint.ptl.day"].sudo()
        lsps = {int(l.lsp_id) for l in lines if l.lsp_id}
        targets = set()
        for company in self.env["res.company"].sudo().search(
                [("dutchie_pos_location_id", "!=", False)]):
            loc = push._resolve_pos_loc_id(company)
            lsp = push._resolve_lsp_id(company)
            if loc and lsp and int(lsp) in lsps:
                targets.add((int(loc), int(lsp)))
        return targets

    @api.model
    def _cron_settle_gift_card_draws(self):
        """Confirm held draws against Dutchie's own report, then retire the
        spent coupons.

        Runs hourly, comfortably inside the stale-hold window, so a real
        redemption is settled before the release sweep could reclaim it.
        Idempotent: it only ever reads rows for codes still in `held`.
        """
        Line = self.env["mint.gift.card.line"].sudo()
        held = Line.search([("state", "=", "held"), ("child_code", "!=", False)])
        if not held:
            self._retire_spent_children()
            return 0

        targets = self._settlement_targets(held)
        if not targets:
            _logger.warning("gift_card settle: %d held draw(s) with no resolvable "
                            "store scope", len(held))
            return 0

        today = fields.Date.context_today(self)
        start = min([(l.held_at.date() if l.held_at else today) for l in held])
        frm = "%d/%d/%d" % (start.month, start.day, start.year)
        to = "%d/%d/%d" % (today.month, today.day, today.year)

        # code -> {order_id, amount}. One fetch per location, reused across
        # every held line, rather than one per (line, location).
        found = {}
        fetched = 0
        for loc, lsp in sorted(targets):
            status, body = self._invsvc_get(
                "/api/admin/discount-usage?locId=%s&lspId=%s&from=%s&to=%s"
                "&reportId=%s&needle=%s"
                % (loc, lsp, urllib.parse.quote(frm), urllib.parse.quote(to),
                   self.USAGE_REPORT_ID, urllib.parse.quote(self.USAGE_NEEDLE))
            )
            if status != 200 or not body.get("ok"):
                continue
            fetched += 1
            for row in body.get("rows") or []:
                code = self._row_get(row, self._USAGE_CODE_KEYS)
                oid = self._row_get(row, self._USAGE_ORDER_KEYS)
                amt = self._row_get(row, self._USAGE_AMOUNT_KEYS)
                if not code or oid is None:
                    continue
                key = str(code).strip().upper()
                slot = found.setdefault(key, {"order_id": str(oid), "amount": 0.0})
                # One redemption emits one row per discounted LINE, so the
                # dollars for a draw are the SUM across that order's rows.
                try:
                    slot["amount"] += float(amt or 0.0)
                except (TypeError, ValueError):
                    pass

        if not fetched:
            _logger.warning("gift_card settle: every report fetch failed — "
                            "leaving %d hold(s) untouched", len(held))
            return 0

        settled = 0
        for line in held:
            hit = found.get((line.child_code or "").strip().upper())
            if not hit:
                continue  # not redeemed (yet). The release sweep handles the rest.
            amount = hit["amount"]
            if line.currency_id.compare_amounts(amount, line.draw_amount) > 0:
                # A single-use, exact-amount coupon should never report more
                # than it was minted for. Cap at what we reserved rather than
                # draining the card, and make the discrepancy loud.
                _logger.error(
                    "gift_card settle: %s reports %s against a %s draw — "
                    "capping at the draw. Investigate order %s.",
                    line.child_code, amount, line.draw_amount, hit["order_id"])
                amount = line.draw_amount
            line.action_settle(amount, order_id=hit["order_id"])
            settled += 1

        self._retire_spent_children()
        _logger.info("gift_card settle: %d of %d held draw(s) settled",
                     settled, len(held))
        return settled

    @api.model
    def _retire_spent_children(self, limit=100):
        """Delete child coupons in Dutchie once their draw is finished.

        Not housekeeping — load-bearing. One store already carries 521
        discounts, that list is pulled on every catalogue sync, and retired
        Dutchie discounts never expire on their own. A gift card programme
        minting one coupon per transaction would swamp it within weeks.
        """
        Line = self.env["mint.gift.card.line"].sudo()
        done = Line.search([
            ("state", "in", ["settled", "released", "failed"]),
            ("child_discount_id", "!=", False),
            ("child_discount_id.is_published", "=", True),
        ], limit=limit)
        retired = 0
        for line in done:
            if line.card_id._retire_child(line.child_discount_id):
                retired += 1
        if retired:
            _logger.info("gift_card settle: retired %d spent child coupon(s)", retired)
        return retired
