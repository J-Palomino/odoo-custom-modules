"""The single LSP resolver: res.company._dutchie_lsp() / mint.region._dutchie_lsp().

Before this existed an LSP could be obtained three ways that agreed only by
luck: the res.company field, a hardcoded map in dutchie_publish matched against
the region's DISPLAY NAME, and assorted raw getattr reads. Renaming a region in
the UI would have silently stopped that region publishing.

res.company.dutchie_lsp_id is now the source of truth; the region derives its
LSP from its own stores; the code-keyed seed map is a last resort. Everything
fails closed at 0 rather than guessing, because guessing writes a discount into
the wrong Dutchie tenant.
"""
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestLspResolver(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Region = self.env['mint.region'].sudo()
        self.Company = self.env['res.company'].sudo()

    # ── store-level ───────────────────────────────────────────────────────
    def test_store_field_is_the_source_of_truth(self):
        s = self.Company.create({'name': 'LSP-R store', 'dutchie_lsp_id': 91234})
        self.assertEqual(s._dutchie_lsp(), 91234)

    def test_store_without_lsp_falls_back_to_its_region(self):
        r = self.Region.create({'name': 'LSP-R Region', 'code': 'ZZ'})
        self.Company.create({
            'name': 'LSP-R sibling', 'region_id': r.id, 'dutchie_lsp_id': 91234,
        })
        bare = self.Company.create({'name': 'LSP-R bare', 'region_id': r.id})
        self.assertEqual(bare._dutchie_lsp(), 91234,
                         'a store missing its LSP should inherit the region it sits in')

    def test_store_fallback_can_be_disabled(self):
        r = self.Region.create({'name': 'LSP-R Region NF', 'code': 'ZY'})
        self.Company.create({
            'name': 'LSP-R sibling NF', 'region_id': r.id, 'dutchie_lsp_id': 91234,
        })
        bare = self.Company.create({'name': 'LSP-R bare NF', 'region_id': r.id})
        self.assertEqual(bare._dutchie_lsp(fallback_to_region=False), 0)

    def test_store_with_no_lsp_and_no_region_is_zero(self):
        s = self.Company.create({'name': 'LSP-R orphan'})
        self.assertEqual(s._dutchie_lsp(), 0, 'must fail closed, never guess')

    # ── region-level ──────────────────────────────────────────────────────
    def test_region_derives_from_its_stores(self):
        r = self.Region.create({'name': 'LSP-R Derive', 'code': 'ZX'})
        for n in ('a', 'b', 'c'):
            self.Company.create({
                'name': 'LSP-R derive %s' % n, 'region_id': r.id,
                'dutchie_lsp_id': 95550,
            })
        self.assertEqual(r._dutchie_lsp(), 95550)

    def test_region_ignores_stores_with_no_lsp(self):
        r = self.Region.create({'name': 'LSP-R Mixed', 'code': 'ZW'})
        self.Company.create({'name': 'LSP-R mixed set', 'region_id': r.id,
                             'dutchie_lsp_id': 95551})
        self.Company.create({'name': 'LSP-R mixed unset', 'region_id': r.id})
        self.assertEqual(r._dutchie_lsp(), 95551)

    def test_region_spanning_two_lsps_fails_closed(self):
        """1:1 has broken — picking one would publish into the wrong tenant."""
        r = self.Region.create({'name': 'LSP-R Split', 'code': 'ZV'})
        self.Company.create({'name': 'LSP-R split 1', 'region_id': r.id,
                             'dutchie_lsp_id': 95552})
        self.Company.create({'name': 'LSP-R split 2', 'region_id': r.id,
                             'dutchie_lsp_id': 95553})
        self.assertEqual(r._dutchie_lsp(), 0)

    def test_region_with_no_stores_is_zero_not_guessed(self):
        """No hardcoded seed: a real region code must NOT conjure an LSP.

        A seed map would answer 575 here. That is precisely the behaviour we
        do not want — it would mask a backfill gap and let a deal publish into
        a guessed tenant instead of failing closed.
        """
        r = self.Region.create({'name': 'Arizona', 'code': 'AZ'})
        self.assertEqual(r._dutchie_lsp(), 0)

    def test_region_lsp_is_never_derived_from_the_name(self):
        r = self.Region.create({'name': 'Arizona', 'code': 'ZU'})
        self.Company.create({'name': 'LSP-R named store', 'region_id': r.id,
                             'dutchie_lsp_id': 91111})
        self.assertEqual(r._dutchie_lsp(), 91111,
                         'the stores decide, never the display name')

    def test_unknown_region_code_is_zero(self):
        r = self.Region.create({'name': 'LSP-R Unknown', 'code': 'QQ'})
        self.assertEqual(r._dutchie_lsp(), 0)

    # ── the push helper delegates rather than reimplementing ──────────────
    def test_push_helper_delegates_to_the_resolver(self):
        s = self.Company.create({'name': 'LSP-R delegate', 'dutchie_lsp_id': 91234})
        self.assertEqual(
            self.env['mint.ptl.day'].sudo()._resolve_lsp_id(s), s._dutchie_lsp())

    def test_push_helper_handles_empty_store(self):
        empty = self.env['res.company'].browse()
        self.assertEqual(self.env['mint.ptl.day'].sudo()._resolve_lsp_id(empty), 0)

    # ── the POS config mirrors the store instead of storing a copy ────────
    def test_web_order_config_mirrors_the_store_lsp(self):
        Config = self.env.get('mint.web.order.config')
        if Config is None:
            self.skipTest('mint_pos_bridge not installed')
        s = self.Company.create({'name': 'LSP-R pos store', 'dutchie_lsp_id': 93210})
        cfg = Config.sudo().create({'company_id': s.id, 'dutchie_loc_id': 1})
        self.assertEqual(cfg.dutchie_lsp_id, 93210)

        # Changing the store must move the config with it — the whole point of
        # dropping the second stored copy.
        s.dutchie_lsp_id = 93211
        cfg.invalidate_recordset(['dutchie_lsp_id'])
        self.assertEqual(cfg.dutchie_lsp_id, 93211)
