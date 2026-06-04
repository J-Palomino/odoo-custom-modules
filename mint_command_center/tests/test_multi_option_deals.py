"""Integration tests for the multi-option deals feature (Odoo #93643).

Covers Phases 2-4: the option mirror on mint.ptl.deal / mint.deal.submission,
the convert_to_deal copy-across, and the plot-time fan-out
(_ensure_discounts) that emits one mint.discount per option.
"""
from odoo.tests import tagged

from .common import MintPtlDealCommon


@tagged("post_install", "-at_install")
class TestMultiOptionDeals(MintPtlDealCommon):

    # ---------- Phase 2: ptl.deal primary-option mirror ----------

    def test_create_with_mirror_fields_seeds_one_option(self):
        deal = self.PtlDeal.create({
            "name": "FA-93643 Mirror Create",
            "brand_id": self.brand.id,
            "product_category": "Flower",
            "discount_type": "percent",
            "discount_value": 20,
        })
        self.assertEqual(len(deal.option_ids), 1)
        opt = deal.option_ids
        self.assertEqual(opt.discount_type, "percent")
        self.assertEqual(opt.discount_value, 20)
        # Mirror reads back the same value
        self.assertEqual(deal.discount_type, "percent")
        self.assertEqual(deal.discount_value, 20)

    def test_inverse_clear_unlinks_primary_option(self):
        deal = self.PtlDeal.create({
            "name": "FA-93643 Clear",
            "brand_id": self.brand.id,
            "discount_type": "percent",
            "discount_value": 20,
        })
        self.assertEqual(len(deal.option_ids), 1)
        deal.write({"discount_type": False})
        # Inverse must unlink the primary option, not just no-op
        self.assertFalse(deal.option_ids)
        self.assertFalse(deal.discount_type)

    def test_inverse_preserves_value_when_only_type_written(self):
        """deal.write({'discount_type': 'percent'}) alone must not zero the option's value."""
        deal = self.PtlDeal.create({
            "name": "FA-93643 Value Passthrough",
            "brand_id": self.brand.id,
            "discount_type": "percent",
            "discount_value": 25,
        })
        deal.write({"discount_type": "percent"})  # idempotent, no value
        self.assertEqual(len(deal.option_ids), 1)
        # Value must NOT have been flattened to 0
        self.assertEqual(deal.option_ids.discount_value, 25)
        self.assertEqual(deal.discount_value, 25)

    def test_inverse_upserts_first_option_when_none_exists(self):
        # Skip the mirror compute by adding options later; create with type to fire inverse
        deal = self.PtlDeal.create({
            "name": "FA-93643 Upsert",
            "brand_id": self.brand.id,
        })
        deal.option_ids.unlink()  # ensure empty
        deal.write({"discount_type": "fixed", "discount_value": 10})
        self.assertEqual(len(deal.option_ids), 1)
        self.assertEqual(deal.option_ids.discount_type, "fixed")
        self.assertEqual(deal.option_ids.discount_value, 10)

    # ---------- Phase 3: submission options + convert ----------

    def _make_submission(self, **overrides):
        Submission = self.env["mint.deal.submission"]
        vals = {
            "name": "FA-93643 Submission",
            "vendor_name": "Test Vendor",
            "vendor_email": "vendor@example.com",
            "brand_id": self.brand.id,
            "product_category": "Flower",
            "state": "approved",
        }
        vals.update(overrides)
        return Submission.create(vals)

    def test_submission_convert_with_two_options(self):
        sub = self._make_submission()
        # Add two options post-create so we control sequence
        sub.option_ids.unlink()
        Option = self.env["mint.deal.submission.option"]
        Option.create({
            "submission_id": sub.id, "sequence": 10,
            "discount_type": "bundle", "discount_value": 30,
        })
        Option.create({
            "submission_id": sub.id, "sequence": 20,
            "discount_type": "bogo", "discount_value": 0,
        })
        self.assertEqual(len(sub.option_ids), 2)

        action = sub.action_convert_to_deal()
        self.assertEqual(sub.state, "converted")
        deal = sub.deal_id
        self.assertTrue(deal)
        self.assertEqual(len(deal.option_ids), 2)
        kinds = sorted(deal.option_ids.mapped("discount_type"))
        self.assertEqual(kinds, ["bogo", "bundle"])
        # action dict shape sanity
        self.assertEqual(action.get("res_model"), "mint.ptl.deal")

    def test_submission_convert_fallback_when_no_options(self):
        """Pre-backfill / option-less submission still converts with one option seeded from mirror."""
        sub = self._make_submission(discount_type="percent", discount_value=15)
        # Force the option-less state (simulate a partially-migrated row)
        sub.option_ids.unlink()
        # Mirror compute would re-add via inverse; simulate by direct DB write
        # of discount_type bypassing the mirror — using cr to mimic legacy data.
        self.env.cr.execute(
            "UPDATE mint_deal_submission SET discount_type='percent', discount_value=15 WHERE id=%s",
            (sub.id,),
        )
        sub.invalidate_recordset(["discount_type", "discount_value", "option_ids"])
        self.assertFalse(sub.option_ids)
        sub.action_convert_to_deal()
        deal = sub.deal_id
        self.assertTrue(deal)
        # Convert fallback seeds one option from the mirror
        self.assertEqual(len(deal.option_ids), 1)
        self.assertEqual(deal.option_ids.discount_type, "percent")
        self.assertEqual(deal.option_ids.discount_value, 15)

    # ---------- Phase 4: plot fan-out ----------

    def _make_deal_with_day(self, options):
        """Return (deal, day) with given option specs [(type, value), ...]."""
        deal = self.PtlDeal.create({
            "name": "FA-93643 Plot",
            "brand_id": self.brand.id,
            "product_category": "Flower",
        })
        deal.option_ids.unlink()
        Option = self.env["mint.ptl.deal.option"]
        for seq, (dt, dv) in enumerate(options, start=1):
            Option.create({
                "ptl_deal_id": deal.id,
                "sequence": seq * 10,
                "discount_type": dt,
                "discount_value": dv,
            })
        day = self.PtlDay.create({
            "date": "2026-06-15",
            "deal_ids": [(6, 0, [deal.id])],
        })
        return deal, day

    def test_plot_single_option_regression(self):
        deal, day = self._make_deal_with_day([("percent", 20)])
        kept = day._ensure_discounts(deal)
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(deal.discount_ids), 1)
        self.assertEqual(
            deal.discount_ids.dutchie_discount_id,
            f"ptl_{deal.id}_opt_{deal.option_ids.id}",
        )

    def test_plot_two_options_produces_two_distinct_discounts(self):
        deal, day = self._make_deal_with_day([("bundle", 30), ("bogo", 0)])
        kept = day._ensure_discounts(deal)
        self.assertEqual(len(kept), 2, "Each option must produce its own mint.discount")
        ddids = sorted(deal.discount_ids.mapped("dutchie_discount_id"))
        expected = sorted(
            f"ptl_{deal.id}_opt_{opt.id}" for opt in deal.option_ids
        )
        self.assertEqual(ddids, expected)

    def test_plot_migration_grace_reuses_legacy_ptl_id(self):
        """Phase 4 reuses a pre-existing 'ptl_{deal.id}' discount for option 0."""
        deal, day = self._make_deal_with_day([("percent", 20), ("bogo", 0)])
        # Pre-seed a legacy single-discount row as if Phase 1-3 never ran
        Discount = self.env["mint.discount"].sudo()
        legacy = Discount.create({
            "name": deal.name,
            "discount_type": "percent",
            "discount_amount": 0.2,
            "is_published": True,
            "ptl_deal_id": deal.id,
            "dutchie_discount_id": f"ptl_{deal.id}",
            "source": "ptl",
        })
        kept = day._ensure_discounts(deal)
        self.assertEqual(len(kept), 2)
        # Legacy id was rewritten to the new _opt_ form for option 0; not orphaned
        self.assertIn(legacy.id, kept.ids)
        self.assertTrue(legacy.dutchie_discount_id.startswith(f"ptl_{deal.id}_opt_"))
        # No discount should be left unpublished
        self.assertTrue(all(d.is_published for d in kept))

    def test_plot_orphan_discount_is_unpublished(self):
        deal, day = self._make_deal_with_day([("percent", 20), ("bogo", 0)])
        # First plot makes 2 discounts
        first = day._ensure_discounts(deal)
        self.assertEqual(len(first), 2)
        # Drop one option, replot — orphan must be unpublished, not deleted
        first_option = deal.option_ids.sorted(lambda o: (o.sequence, o.id))[0]
        first_option_ddid = f"ptl_{deal.id}_opt_{first_option.id}"
        first_option.unlink()
        kept = day._ensure_discounts(deal)
        self.assertEqual(len(kept), 1)
        # The orphan still exists, but is_published=False
        Discount = self.env["mint.discount"].sudo()
        orphan = Discount.search([("dutchie_discount_id", "=", first_option_ddid)])
        self.assertEqual(len(orphan), 1)
        self.assertFalse(orphan.is_published)
        # The kept one is still published
        self.assertTrue(kept.is_published)
