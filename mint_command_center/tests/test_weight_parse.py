from odoo.tests import TransactionCase, tagged

from ..models.brand_calendar import _parse_weight


@tagged("post_install", "-at_install")
class TestParseWeight(TransactionCase):
    """Regression tests for _parse_weight.

    This function had no test coverage, which is how a 10x error shipped: the
    old pattern `\\b(\\d*\\.?\\d+)\\s*(mg|g|oz)\\b` put the word boundary
    BETWEEN the dot and the digit of a bare decimal, so ".5g" matched from the
    "5" and parsed as 5g. Measured on production 2026-08-24, 124 deals carried
    a leading-dot weight and every one stored the wrong value.

    The parsed value feeds mint.ptl.deal.weight_value, which
    dutchie_discount_push._resolve_weight_restriction converts to grams and
    publishes as the Dutchie Weight restriction — so a wrong parse scopes the
    deal to the wrong products at the register.
    """

    def test_leading_dot_decimal(self):
        """".5g" is half a gram, not five."""
        for title in (
            "WTF - Cartridge Live Resin .5g",
            "$5 WTF Diamond Infused Pre-Rolls .5g EDLP",
            "Tsunami Live Resin Concentrates .5g",
            "Dadirri - Infused Preroll .5g - 40% off",
        ):
            self.assertEqual(_parse_weight(title), (0.5, "g"), title)

    def test_leading_dot_other_units(self):
        self.assertEqual(_parse_weight("Tincture .5oz"), (0.5, "oz"))
        self.assertEqual(_parse_weight("Mint .5mg microdose"), (0.5, "mg"))

    def test_standard_decimals_unchanged(self):
        """A digit before the dot must still parse from that digit, not the
        fractional part — the lookbehind must not shift the match."""
        self.assertEqual(_parse_weight("Aeriz Flower 3.5g"), (3.5, "g"))
        self.assertEqual(_parse_weight("Diablo Premium Flower 7.77g"), (7.77, "g"))
        self.assertEqual(_parse_weight("The Pharm Cured Crumble 1.25g"), (1.25, "g"))
        self.assertEqual(_parse_weight("Cartridge 0.5g"), (0.5, "g"))

    def test_integers_and_units(self):
        self.assertEqual(_parse_weight("MC Shake 14g"), (14.0, "g"))
        self.assertEqual(_parse_weight("Mint Popcorn - Silver - 28g"), (28.0, "g"))
        self.assertEqual(_parse_weight("TRU-Infusion Gummies 100mg"), (100.0, "mg"))

    def test_mass_beats_count(self):
        """Documented rule: mass units win over count units in one string."""
        self.assertEqual(_parse_weight("Shorties Prerolls 10pk - (5.0g)"), (5.0, "g"))

    def test_pack_normalises_to_count(self):
        self.assertEqual(_parse_weight("Preroll 5pk"), (5.0, "ct"))
        self.assertEqual(_parse_weight("Sublime Gummies 10ct"), (10.0, "ct"))

    def test_price_text_is_not_a_weight(self):
        """Money in the title must not be mistaken for a weight."""
        self.assertEqual(
            _parse_weight("$41.80 -> $20.90 45% Off Stiiizy Infused Blunts 2g"),
            (2.0, "g"),
        )

    def test_no_weight_returns_empty(self):
        self.assertEqual(_parse_weight("BOGO Flavors Cannabis Co"), (0.0, False))
        self.assertEqual(_parse_weight(""), (0.0, False))
        self.assertEqual(_parse_weight(None), (0.0, False))

    def test_falls_through_multiple_sources(self):
        """_parse_weight takes *sources and uses the first that yields a hit."""
        self.assertEqual(_parse_weight(None, "", "Live Resin .5g"), (0.5, "g"))
        self.assertEqual(_parse_weight("no weight here", "AIO 2g"), (2.0, "g"))
