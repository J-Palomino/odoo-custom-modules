"""The filename rules that keep COA links unambiguous.

A duplicate certificate name is not cosmetic: /coa derives its batch tokens from
the filename and normalises them, so two files that normalise alike both answer
the same batch search as "exact" hits, the page stops auto-opening, and whoever
scanned the label picks between identical-looking rows. Production already
carries such pairs, inherited from the Google Drive migration.

These are the pure helpers, so they are cheap to assert exhaustively; the Odoo
round-trips around them live in ``controllers/main.py``.
"""

import re

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase

from ..controllers.main import (
    _clash_in, _matches_query, _search_fragments, _search_key, _stem, _unique_name,
)


class TestCoaNameCollisions(TransactionCase):
    FOLDER = [
        {"id": 1, "name": "AERIZ 20240130PRM-5T7 XD 20240411.pdf"},
        {"id": 2, "name": "WYLD AZ SB B126.pdf"},
    ]

    def test_search_key_matches_the_coa_page(self):
        """It must reproduce norm() in src/pages/coa.astro — that function is
        what decides whether a customer's search can tell two files apart."""
        def norm(value):
            return re.sub(r"[^A-Z0-9]", "", value.upper())

        for value in ["WYLD AZ SB B126.pdf", "251230-003",
                      "AERIZ 20250514FLOGMO-2T8LR XD:20250611.pdf"]:
            self.assertEqual(_search_key(value), norm(value))

    def test_variants_that_search_cannot_separate_are_clashes(self):
        # All of these reduce to WYLDAZSBB126PDF.
        for variant in ["wyld az sb b126.pdf", "WYLD-AZ-SB-B126.pdf",
                        "WYLD  AZ SB B126.pdf", "WYLDAZSBB126.pdf",
                        "  WYLD AZ SB B126.pdf "]:
            self.assertEqual(_clash_in(self.FOLDER, variant)["id"], 2, variant)

    def test_a_genuinely_different_batch_is_allowed(self):
        self.assertIsNone(_clash_in(self.FOLDER, "WYLD AZ SB B127.pdf"))
        self.assertIsNone(_clash_in(self.FOLDER, "WYLD AZ SB B1266.pdf"))

    def test_a_file_may_keep_its_own_name(self):
        """Renaming or moving a file in place must not clash with itself."""
        self.assertIsNone(_clash_in(self.FOLDER, "WYLD AZ SB B126.pdf", 2))

    def test_a_name_with_nothing_to_match_on_never_clashes(self):
        self.assertIsNone(_clash_in([{"id": 1, "name": "---"}], "***"))

    def test_unique_name_follows_the_dms_convention(self):
        """dms/tools/file.py numbers copies as "name(2).ext"."""
        self.assertEqual(_unique_name([{"id": 1, "name": "A.pdf"}], "A.pdf"), "A(2).pdf")

    def test_unique_name_skips_suffixes_already_taken(self):
        rows = [{"id": 1, "name": "A.pdf"}, {"id": 2, "name": "a(2).pdf"},
                {"id": 3, "name": "A(3).PDF"}]
        self.assertEqual(_unique_name(rows, "A.pdf"), "A(4).pdf")

    def test_unique_name_handles_an_extensionless_name(self):
        self.assertEqual(_unique_name([{"id": 1, "name": "A"}], "A"), "A(2)")

    def test_unique_name_gives_up_loudly(self):
        rows = [{"id": 0, "name": "A.pdf"}]
        rows += [{"id": i, "name": "A(%d).pdf" % i} for i in range(2, 500)]
        with self.assertRaises(UserError):
            _unique_name(rows, "A.pdf")

    def test_stem_splits_on_the_last_dot_only(self):
        self.assertEqual(_stem("A.v2.pdf"), ("A.v2", ".pdf"))
        self.assertEqual(_stem("ABC"), ("ABC", ""))
        self.assertEqual(_stem(".hidden"), (".hidden", ""))


class TestCoaStaffSearch(TransactionCase):
    """Staff search matches on the same key as /coa, so the certificate a
    customer finds by batch number is the one staff find to replace."""

    NAME = "22RED 01062026-ACLR.pdf"

    def test_finds_a_certificate_however_it_is_punctuated(self):
        for query in ["01062026ACLR", "01062026-ACLR", "0106 2026 aclr",
                      "22red 01062026aclr"]:
            self.assertTrue(_matches_query(self.NAME, query), query)

    def test_matches_a_fragment(self):
        self.assertTrue(_matches_query(self.NAME, "01062026"))

    def test_does_not_match_an_unrelated_certificate(self):
        self.assertFalse(_matches_query(self.NAME, "01062026XXXX"))

    def test_an_empty_query_is_never_match_all(self):
        for query in ["", "   ", "---"]:
            self.assertFalse(_matches_query(self.NAME, query), repr(query))

    def test_ands_the_runs_when_separators_were_typed(self):
        self.assertEqual(_search_fragments("01062026-ACLR"), (["01062026", "ACLR"], "and"))

    def test_ors_windows_for_an_unpunctuated_query(self):
        """The case that regressed: the whole string is not ilike-found in
        "22RED 01062026ACLR.pdf", but a window inside a run is."""
        fragments, mode = _search_fragments("22RED01062026ACLR")
        self.assertEqual(mode, "or")
        self.assertGreater(len(fragments), 1)
        self.assertTrue(any(f in "01062026ACLR" for f in fragments))

    def test_no_fragment_ever_spans_a_separator(self):
        """A fragment that straddles a separator would drop a true hit."""
        for query in ["01062026-ACLR", "0106 2026 aclr", "WYLD BO B41 1 THC W41",
                      "22RED01062026ACLR"]:
            runs = re.findall(r"[A-Za-z0-9]+", query)
            for frag in _search_fragments(query)[0]:
                self.assertTrue(any(frag.upper() in r.upper() for r in runs),
                                "%s from %s" % (frag, query))

    def test_the_coarse_filter_retrieves_a_real_filename_however_typed(self):
        stored = "22RED 01062026ACLR.pdf"
        for query in ["22RED 01062026ACLR", "22red 01062026aclr",
                      "22RED01062026ACLR", "22RED-01062026ACLR"]:
            fragments, mode = _search_fragments(query)
            up = stored.upper()
            retrieved = (all(f.upper() in up for f in fragments) if mode == "and"
                         else any(f.upper() in up for f in fragments))
            self.assertTrue(retrieved, "coarse filter dropped %r" % query)
            self.assertTrue(_matches_query(stored, query))

    def test_a_short_run_is_used_as_is(self):
        self.assertEqual(_search_fragments("B126"), (["B126"], "and"))

    def test_nothing_to_filter_on_for_an_empty_query(self):
        self.assertEqual(_search_fragments("   ")[0], [])
        self.assertEqual(_search_fragments("--- ...")[0], [])
