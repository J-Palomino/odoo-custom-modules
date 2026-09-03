from odoo.tests.common import TransactionCase, tagged

from ..models.daisy_agent import _DEFAULT_REPLY_STYLE, _REPLY_STYLE_PARAM


@tagged("post_install", "-at_install")
class TestContextPrefixReplyStyle(TransactionCase):
    """The reply-style directive injected into every agent question.

    Agent replies are posted to chatter verbatim, and Agentflow V2 accepts
    neither ``overrideConfig.startState`` nor per-call ``vars`` — so the
    question text is the only channel that can constrain reply length. These
    tests are the contract for that channel.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.agent = cls.env["daisy.agent"].create(
            {"name": "Test Agent Style", "code": "teststyle"}
        )
        cls.ICP = cls.env["ir.config_parameter"].sudo()

    def _prefix(self, **kw):
        return self.agent._build_context_prefix(**kw)

    def test_style_present_by_default(self):
        self.assertIn(_DEFAULT_REPLY_STYLE, self._prefix())

    def test_style_emitted_even_when_nothing_else_is_known(self):
        """A directive is still correct with no speaker/session context.

        The identity lines stay conditional — this asserts the block is no
        longer empty, which is the one behaviour this change deliberately flips.
        """
        prefix = self._prefix()
        self.assertTrue(prefix)
        self.assertNotIn("[Speaking with:", prefix)
        self.assertNotIn("[Session:", prefix)

    def test_style_is_last_so_it_sits_closest_to_the_question(self):
        prefix = self._prefix(session_id="partner-1")
        self.assertLess(prefix.index("[Session:"), prefix.index(_DEFAULT_REPLY_STYLE))

    def test_identity_lines_still_rendered(self):
        prefix = self._prefix(session_id="partner-7")
        self.assertIn("[Session: partner-7]", prefix)
        self.assertIn(_DEFAULT_REPLY_STYLE, prefix)

    def test_config_parameter_overrides_the_default(self):
        self.ICP.set_param(_REPLY_STYLE_PARAM, "[Reply style: terse.]")
        prefix = self._prefix()
        self.assertIn("[Reply style: terse.]", prefix)
        self.assertNotIn(_DEFAULT_REPLY_STYLE, prefix)

    def test_empty_config_parameter_disables_the_directive(self):
        self.ICP.set_param(_REPLY_STYLE_PARAM, "")
        prefix = self._prefix()
        self.assertNotIn("Reply style", prefix)
        # ...and with no other context the block collapses to empty again.
        self.assertEqual(prefix, "")

    def test_whitespace_only_parameter_also_disables(self):
        self.ICP.set_param(_REPLY_STYLE_PARAM, "   ")
        self.assertEqual(self._prefix(), "")

    def test_prefix_ends_with_blank_line_separator(self):
        """The job concatenates prefix + message_text directly."""
        self.assertTrue(self._prefix().endswith("\n\n"))
