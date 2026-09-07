from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestPrivateAgentAllowlist(TransactionCase):
    """Access control for PRIVATE daisy agents.

    A private agent replies only to its owner plus the users on
    ``x_private_allowed_ids``. Everyone else is dropped SILENTLY — no job, no
    reply, no error — which is exactly why this needs tests: a regression here
    is invisible in the logs and only shows up as an agent that "doesn't
    respond" (HUGO / Douglas Schwartz, 2026-08-27).

    Both auto-reply gates and the @mention hide-predicate share these helpers,
    so "can see the agent" and "gets a reply from it" cannot drift apart.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env["res.users"].with_context(no_reset_password=True)
        cls.owner = Users.create({"name": "Gate Owner", "login": "gate_owner"})
        cls.friend = Users.create({"name": "Gate Friend", "login": "gate_friend"})
        cls.stranger = Users.create({"name": "Gate Stranger", "login": "gate_stranger"})
        cls.agent = cls.env["daisy.agent"].create(
            {"name": "Gate Agent", "code": "gateagent"}
        )

    def _make_private(self, allowed=None):
        self.agent.write({
            "x_private_owner_id": self.owner.id,
            "x_private_allowed_ids": [(6, 0, (allowed or self.env["res.users"]).ids)],
        })

    # --- non-private agents are unaffected --------------------------------

    def test_shared_agent_blocks_nobody(self):
        self.assertFalse(self.agent.x_private_owner_id)
        for user in (self.owner, self.friend, self.stranger):
            self.assertFalse(self.agent._is_private_blocked(user.partner_id))

    def test_allowlist_without_owner_does_not_make_agent_private(self):
        """The allowlist is meaningless on its own — owner is the on-switch.

        Guards against a half-configured agent silently going private and
        going quiet for everyone not on the list.
        """
        self.agent.write({"x_private_allowed_ids": [(6, 0, self.friend.ids)]})
        self.assertFalse(self.agent.x_private_owner_id)
        self.assertFalse(self.agent._is_private_blocked(self.stranger.partner_id))
        self.assertFalse(self.agent._is_private_hidden_from_user(self.stranger))

    # --- owner-only (pre-allowlist behaviour must be preserved) -----------

    def test_owner_always_reaches_private_agent(self):
        self._make_private()
        self.assertFalse(self.agent._is_private_blocked(self.owner.partner_id))

    def test_stranger_blocked_when_allowlist_empty(self):
        self._make_private()
        self.assertTrue(self.agent._is_private_blocked(self.stranger.partner_id))
        self.assertTrue(self.agent._is_private_blocked(self.friend.partner_id))

    # --- the allowlist itself ---------------------------------------------

    def test_allowlisted_user_reaches_private_agent(self):
        self._make_private(allowed=self.friend)
        self.assertFalse(self.agent._is_private_blocked(self.friend.partner_id))

    def test_allowlist_does_not_open_the_agent_to_everyone(self):
        self._make_private(allowed=self.friend)
        self.assertTrue(self.agent._is_private_blocked(self.stranger.partner_id))

    def test_owner_still_reaches_agent_that_has_an_allowlist(self):
        self._make_private(allowed=self.friend)
        self.assertFalse(self.agent._is_private_blocked(self.owner.partner_id))

    def test_removing_a_user_from_the_allowlist_blocks_them_again(self):
        self._make_private(allowed=self.friend)
        self.assertFalse(self.agent._is_private_blocked(self.friend.partner_id))
        self.agent.write({"x_private_allowed_ids": [(5, 0, 0)]})
        self.assertTrue(self.agent._is_private_blocked(self.friend.partner_id))

    # --- fail-closed -------------------------------------------------------

    def test_authorless_message_is_blocked(self):
        """Inbound email / livechat guests have no author partner.

        ``empty not in recordset`` must read as blocked, preserving the
        original gate's fail-closed behaviour.
        """
        self._make_private(allowed=self.friend)
        self.assertTrue(
            self.agent._is_private_blocked(self.env["res.partner"].browse())
        )

    def test_allowed_partners_is_empty_for_shared_agent(self):
        """Emptiness means "not applicable", never "nobody" — see the helper."""
        self.assertFalse(self.agent._private_allowed_partners())
        self._make_private(allowed=self.friend)
        self.assertEqual(
            self.agent._private_allowed_partners(),
            self.owner.partner_id | self.friend.partner_id,
        )

    # --- visibility mirrors reachability -----------------------------------

    def test_hidden_from_stranger_only(self):
        self._make_private(allowed=self.friend)
        self.assertTrue(self.agent._is_private_hidden_from_user(self.stranger))
        self.assertFalse(self.agent._is_private_hidden_from_user(self.owner))
        self.assertFalse(self.agent._is_private_hidden_from_user(self.friend))

    def test_visibility_and_reachability_agree(self):
        """The two gates must never disagree: anyone who can see the agent can
        reach it, and anyone hidden from it is blocked."""
        self._make_private(allowed=self.friend)
        for user in (self.owner, self.friend, self.stranger):
            self.assertEqual(
                self.agent._is_private_hidden_from_user(user),
                self.agent._is_private_blocked(user.partner_id),
                f"gates disagree for {user.name}",
            )
