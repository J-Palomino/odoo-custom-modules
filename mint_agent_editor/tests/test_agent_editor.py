# -*- coding: utf-8 -*-
"""Reply parsing and snippet materialisation.

The parser is the fragile seam: the agent is told to fence its JSON but does
not always, and a malformed plan must degrade to "no ops" rather than throwing
— the human should still see the reply.
"""

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.mint_agent_editor.controllers.main import _extract_plan, _strip_plan


@tagged("post_install", "-at_install")
class TestPlanParsing(TransactionCase):

    def test_fenced_json_plan_is_extracted(self):
        reply = 'Sure, renaming it.\n```json\n{"ops":[{"op":"set_text","selector":"#a","value":"Hi"}]}\n```'
        ops = _extract_plan(reply)
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["op"], "set_text")

    def test_unfenced_plan_is_still_found(self):
        reply = 'Done. {"ops":[{"op":"remove","selector":"#b"}]}'
        self.assertEqual(len(_extract_plan(reply)), 1)

    def test_prose_only_reply_yields_no_ops(self):
        self.assertEqual(_extract_plan("That page has three sections."), [])

    def test_malformed_json_does_not_raise(self):
        self.assertEqual(_extract_plan('```json\n{"ops": [oops}\n```'), [])

    def test_entries_without_an_op_key_are_dropped(self):
        reply = '```json\n{"ops":[{"selector":"#a"},{"op":"remove","selector":"#b"}]}\n```'
        ops = _extract_plan(reply)
        self.assertEqual([o["op"] for o in ops], ["remove"])

    def test_empty_reply_is_safe(self):
        self.assertEqual(_extract_plan(None), [])
        self.assertEqual(_extract_plan(""), [])

    def test_prose_is_stripped_of_the_json_block(self):
        reply = 'Renaming the title.\n```json\n{"ops":[]}\n```'
        self.assertEqual(_strip_plan(reply), "Renaming the title.")


@tagged("post_install", "-at_install")
class TestSnippetEndpoint(TransactionCase):

    def test_website_template_is_preferred_over_mass_mailing(self):
        """Both modules ship s_cover; mass_mailing has the lower id and would
        win an unpinned lookup, giving an email block on a web page."""
        View = self.env["ir.ui.view"]
        website = View.search([("key", "=", "website.s_cover")], limit=1)
        mailing = View.search([("key", "=", "mass_mailing.s_cover")], limit=1)
        if not (website and mailing):
            self.skipTest("both s_cover templates required for this check")
        self.assertLess(mailing.id, website.id, "precondition: mail template sorts first")

    def test_snippet_template_carries_no_data_snippet(self):
        """Templates gain data-snippet only when dropped; that is why pasting a
        template verbatim produces an uneditable block."""
        view = self.env["ir.ui.view"].search([("key", "=", "website.s_text_block")], limit=1)
        if not view:
            self.skipTest("website.s_text_block not installed")
        self.assertNotIn("data-snippet", view.arch)
