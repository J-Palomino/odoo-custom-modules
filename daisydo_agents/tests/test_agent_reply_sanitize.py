from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

from ..models import daisy_agent_job as job_module


@tagged("post_install", "-at_install")
class TestAgentReplySanitize(TransactionCase):
    """MR-954 — outbound-link lint + superseded-job guard.

    The sanitizer replaces the Flowise link-gate (downstream flow nodes
    disable tool execution platform-wide), so these tests are the contract
    for every link an agent posts.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Job = cls.env["daisy.agent.job"]
        cls.agent = cls.env["daisy.agent"].create(
            {"name": "Test Agent MR954", "code": "testmr954"}
        )
        cls.partner = cls.env["res.partner"].create({"name": "MR954 Link Target"})
        # A real act_window whose res_model has a real record — the allowed
        # link is built dynamically so the test holds on any database.
        cls.action = cls.env["ir.actions.act_window"].create(
            {"name": "MR954 Partners", "res_model": "res.partner"}
        )
        cls.good_url = (
            f"https://letsgomint.us/odoo/action-{cls.action.id}/{cls.partner.id}"
        )

    def _sanitize(self, text):
        return self.Job._sanitize_agent_reply(text, check_user=self.env.user)

    # --- AC02: valid action links pass through byte-identical ---
    def test_valid_link_untouched(self):
        text = f"Source: GL sheet — {self.good_url}"
        self.assertEqual(self._sanitize(text), text)

    def test_valid_link_with_trailing_punctuation(self):
        text = f"See {self.good_url}, then reply."
        self.assertEqual(self._sanitize(text), text)

    # --- AC01: non-allowlisted letsgomint odoo URL is stripped + noted ---
    def test_bad_route_stripped(self):
        out = self._sanitize(
            "Here: https://letsgomint.us/odoo/apps/some/other/route"
        )
        self.assertIn(job_module.LINK_REMOVED_MARKER, out)
        self.assertNotIn("/odoo/apps", out)
        self.assertIn("link(s) were removed", out)

    def test_nonexistent_action_stripped(self):
        out = self._sanitize(
            "Ticket: https://letsgomint.us/odoo/action-99999999/1"
        )
        self.assertIn(job_module.LINK_REMOVED_MARKER, out)

    # --- AC04 (Tier 2): link to a nonexistent record is stripped ---
    def test_nonexistent_record_stripped(self):
        out = self._sanitize(
            f"Rec: https://letsgomint.us/odoo/action-{self.action.id}/99999999"
        )
        self.assertIn(job_module.LINK_REMOVED_MARKER, out)

    def test_tier2_kill_switch(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "daisydo_agents.link_check_tier2", "0"
        )
        url = f"https://letsgomint.us/odoo/action-{self.action.id}/99999999"
        try:
            # Form is valid; with tier2 off the dead record is NOT checked.
            self.assertEqual(self._sanitize(f"Rec: {url}"), f"Rec: {url}")
        finally:
            self.env["ir.config_parameter"].sudo().set_param(
                "daisydo_agents.link_check_tier2", "1"
            )

    # --- AC03: markdown links rewritten to "label — url" ---
    def test_markdown_link_rewritten(self):
        out = self._sanitize(f"[GL sheet]({self.good_url})")
        self.assertEqual(out, f"GL sheet — {self.good_url}")

    def test_markdown_link_external_domain_rewritten_not_stripped(self):
        out = self._sanitize("[docs](https://example.com/a?b=1)")
        self.assertEqual(out, "docs — https://example.com/a?b=1")

    # --- non-letsgomint URLs are untouched ---
    def test_external_url_untouched(self):
        text = "See https://github.com/FlowiseAI/Flowise/issues/4459 for context"
        self.assertEqual(self._sanitize(text), text)

    # --- AC08: sanitizer failure fails OPEN (posts original) ---
    def test_fail_open_on_internal_error(self):
        text = f"Body {self.good_url}"
        with patch.object(
            job_module.ODOO_URL_RE, "sub", side_effect=RuntimeError("boom")
        ):
            self.assertEqual(self._sanitize(text), text)

    def test_empty_and_none_input(self):
        self.assertEqual(self._sanitize(""), "")
        self.assertEqual(self._sanitize(None), "")

    # --- AC05: superseded-job guard ---
    def test_is_superseded(self):
        base = {
            "agent_id": self.agent.id,
            "channel_model": "discuss.channel",
            "channel_id": 12345,
            "message_text": "first",
        }
        older = self.Job.create(base)
        self.assertFalse(older._is_superseded(), "sole job is not superseded")
        newer = self.Job.create(dict(base, message_text="second"))
        # Same-second create_dates are broken by id ordering.
        self.assertTrue(older._is_superseded())
        self.assertFalse(newer._is_superseded())
        # A newer job on a DIFFERENT channel does not supersede.
        other = self.Job.create(
            dict(base, channel_id=99999, message_text="elsewhere")
        )
        self.assertFalse(newer._is_superseded())
        self.assertFalse(other._is_superseded())
        # An errored newer job does not supersede (it never replied).
        newest = self.Job.create(dict(base, message_text="third"))
        newest.write({"state": "error", "error_message": "x"})
        self.assertFalse(newer._is_superseded())

    # --- AC06: history helper returns the NEWEST N, chronological ---
    def test_recent_history_newest_n(self):
        Msg = self.env["mail.message"]
        target = self.env["res.partner"].create({"name": "MR954 History"})
        for i in range(25):
            Msg.create(
                {
                    "model": "res.partner",
                    "res_id": target.id,
                    "body": f"<p>msg {i}</p>",
                    "message_type": "comment",
                }
            )
        msgs = self.Job._recent_history_messages(
            [("model", "=", "res.partner"), ("res_id", "=", target.id)], 20
        )
        self.assertEqual(len(msgs), 20)
        bodies = [m.body for m in msgs]
        self.assertIn("msg 24", bodies[-1], "newest message must be last")
        self.assertNotIn("msg 0", " ".join(bodies), "oldest must be dropped")
        self.assertEqual(
            bodies, sorted(bodies, key=lambda b: int(b.split("msg ")[1].split("<")[0])),
            "history must be chronological",
        )
