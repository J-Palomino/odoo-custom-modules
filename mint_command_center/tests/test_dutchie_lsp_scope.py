"""Regression: Dutchie discounts are LSP-scoped, not per-location.

Verified read-only against Dutchie via mintinvsvc
``GET /api/admin/discounts/<id>?locId=&lspId=``:

  * discount 385159, created addressing loc 2679 (AZ - 75th Ave), resolves to
    the SAME record when fetched with loc 1568 (AZ - Tempe) under lsp 575;
  * the same id under a different lsp (820, NV) returns HTTP 401
    "User is not permitted to perform this action";
  * an absent id returns ``{"reason": "not_found_in_dutchie"}``.

So the LSP owns the record; locId is only the addressing handle the API
requires. Two bugs followed from believing otherwise:

  1. ``_deal_to_dutchie_payload`` resolved the existing Dutchie Id from the push
     log keyed on ``(discount, store)``. Pushing one discount via a second store
     in the same LSP missed, resolved Id=0, and CREATED a duplicate LSP-wide
     record sharing the DiscountCode and carrying its own MaxRedemptions
     counter. Five pairs exist in lsp 575, e.g. 385159 + 385236 for coupon 3046
     (both code MINT-58N83Z, MaxRedemptions=1).
  2. The publish paths fanned out once per store, so under a shared LSP every
     extra store was a redundant write — and the trigger for (1).

These tests pin the LSP keying and the deterministic collapse.
"""
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestDutchieLspScope(TransactionCase):

    def setUp(self):
        super().setUp()
        self.push = self.env['mint.ptl.day'].sudo()
        Company = self.env['res.company'].sudo()
        # Two stores sharing one LSP (the AZ 575 shape) plus one on its own.
        self.a = Company.create({
            'name': 'TEST LSP-A store 1',
            'dutchie_lsp_id': 90575,
            'dutchie_pos_location_id': 92679,
        })
        self.b = Company.create({
            'name': 'TEST LSP-A store 2',
            'dutchie_lsp_id': 90575,
            'dutchie_pos_location_id': 91568,
        })
        self.c = Company.create({
            'name': 'TEST LSP-B store',
            'dutchie_lsp_id': 90820,
            'dutchie_pos_location_id': 91111,
        })

    # ── collapse ──────────────────────────────────────────────────────────
    def test_collapse_picks_one_store_per_lsp(self):
        got = self.push._collapse_stores_by_lsp(self.a | self.b | self.c)
        self.assertEqual(len(got), 2, 'two distinct LSPs -> two writes')
        self.assertIn(self.c, got)
        self.assertTrue((self.a in got) != (self.b in got),
                        'exactly one of the shared-LSP pair survives')

    def test_collapse_is_deterministic_lowest_id(self):
        # The drifting pick is what created 385159 + 385236 six days apart.
        first = self.push._collapse_stores_by_lsp(self.a | self.b)
        second = self.push._collapse_stores_by_lsp(self.b | self.a)
        self.assertEqual(first.ids, second.ids)
        self.assertIn(self.a, first, 'lowest id wins regardless of input order')

    def test_collapse_keeps_stores_without_an_lsp(self):
        Company = self.env['res.company'].sudo()
        noloc = Company.create({'name': 'TEST no LSP', 'dutchie_lsp_id': 0})
        got = self.push._collapse_stores_by_lsp(self.a | self.b | noloc)
        self.assertIn(noloc, got,
                      'LSP-less stores must survive so the backfill warning fires')
        self.assertEqual(len(got), 2)

    def test_collapse_single_store_is_identity(self):
        self.assertEqual(self.push._collapse_stores_by_lsp(self.a).ids, self.a.ids)

    # ── id resolution keyed on LSP ────────────────────────────────────────
    def _discount(self):
        return self.env['mint.discount'].sudo().create({
            'name': 'TEST lsp scope',
            'source': 'ptl',
            'discount_type': 'percent',
            'discount_amount': 0.3,
        })

    def _log(self, discount, store, dutchie_id):
        return self.env['mint.dutchie.discount.push.log'].sudo().create({
            'discount_id': discount.id,
            'company_id': store.id,
            'mode': 'live',
            'success': True,
            'dutchie_discount_id': dutchie_id,
        })

    def test_sibling_store_reuses_the_lsp_discount_id(self):
        """The core fix: store B must UPDATE store A's record, not create."""
        d = self._discount()
        self._log(d, self.a, 385159)
        payload = self.push._deal_to_dutchie_payload(d, self.b)
        self.assertEqual(
            payload['Id'], 385159,
            'a sibling store in the same LSP must resolve the existing id; '
            'Id=0 here is exactly what created the 385236 duplicate')

    def test_other_lsp_does_not_reuse_the_id(self):
        d = self._discount()
        self._log(d, self.a, 385159)
        payload = self.push._deal_to_dutchie_payload(d, self.c)
        self.assertEqual(payload['Id'], 0,
                         'a different LSP is a different record — must create')

    def test_no_prior_push_creates(self):
        d = self._discount()
        self.assertEqual(self.push._deal_to_dutchie_payload(d, self.a)['Id'], 0)

    def test_dry_run_log_does_not_resolve_an_id(self):
        d = self._discount()
        log = self._log(d, self.a, 385159)
        log.write({'mode': 'dry-run'})
        self.assertEqual(self.push._deal_to_dutchie_payload(d, self.b)['Id'], 0,
                         'only a successful LIVE push establishes a real id')
