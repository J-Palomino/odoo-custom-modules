# -*- coding: utf-8 -*-
"""Authorization-code flow tests.

These target the failure modes that turn an OAuth bug into account takeover:
code replay, PKCE bypass, redirect-URI substitution, and access surviving a
revoked group membership.
"""
import base64
import hashlib

from odoo.tests import TransactionCase, tagged

from ..controllers.flow import MintOidcFlow

REDIRECT = "https://ux.letsgomint.us/api/auth/oauth/oidc/callback"


def challenge_for(verifier):
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@tagged("post_install", "-at_install", "mint_oauth")
class TestOidcFlow(TransactionCase):

    def setUp(self):
        super().setUp()
        self.client = self.env["mint.oauth.client"].create({
            "name": "Penpot",
            "redirect_uri_ids": [(0, 0, {"uri": REDIRECT})],
        })
        self.secret = self.client._new_secret()
        self.user = self.env["res.users"].create({
            "name": "Designer", "login": "designer@themintdispensary.com",
            "email": "designer@themintdispensary.com",
        })
        self.user.group_ids = [(4, self.env.ref("mint_oauth_provider.group_oidc_user").id)]
        self.Code = self.env["mint.oauth.code"]
        self.Token = self.env["mint.oauth.token"]

    def _issue_code(self, verifier="verifier-0123456789-abcdefghij"):
        return self.Code._issue(
            client=self.client, user=self.user, redirect_uri=REDIRECT,
            scope="openid email profile", nonce="nonce-1",
            code_challenge=challenge_for(verifier), code_challenge_method="S256",
        )

    # -- PKCE ----------------------------------------------------------
    def test_pkce_accepts_matching_verifier(self):
        verifier = "verifier-0123456789-abcdefghij"
        self.assertTrue(MintOidcFlow._pkce_ok(verifier, challenge_for(verifier)))

    def test_pkce_rejects_mismatch_and_empties(self):
        good = challenge_for("verifier-0123456789-abcdefghij")
        for verifier, challenge in (
            ("wrong-verifier", good),
            ("", good),
            (None, good),
            ("verifier-0123456789-abcdefghij", ""),
            ("verifier-0123456789-abcdefghij", None),
            ("", ""),
        ):
            self.assertFalse(
                MintOidcFlow._pkce_ok(verifier, challenge),
                "PKCE should have failed for %r/%r" % (verifier, challenge),
            )

    def test_pkce_plain_verifier_does_not_pass_as_challenge(self):
        """A 'plain' style challenge (verifier echoed) must not validate."""
        verifier = "verifier-0123456789-abcdefghij"
        self.assertFalse(MintOidcFlow._pkce_ok(verifier, verifier))

    # -- authorization codes -------------------------------------------
    def test_code_is_single_use(self):
        raw = self._issue_code()
        first = self.Code._consume(raw, self.client)
        self.assertTrue(first)
        self.assertTrue(first.used_at)
        # Second attempt must fail — this is the replay case.
        self.assertFalse(self.Code._consume(raw, self.client))

    def test_code_replay_revokes_descendant_tokens(self):
        raw = self._issue_code()
        record = self.Code._consume(raw, self.client)
        self.Token._issue(self.client, self.user, "openid",
                          token_type="access", code=record)
        self.Token._issue(self.client, self.user, "openid",
                          token_type="refresh", code=record)
        live = self.Token.search([("code_id", "=", record.id), ("revoked", "=", False)])
        self.assertEqual(len(live), 2)

        # Replaying the code means we cannot tell attacker from victim, so the
        # whole grant family dies.
        self.assertFalse(self.Code._consume(raw, self.client))
        still_live = self.Token.search([
            ("code_id", "=", record.id), ("revoked", "=", False)
        ])
        self.assertFalse(still_live, "replay must revoke every descendant token")

    def test_code_not_redeemable_by_another_client(self):
        other = self.env["mint.oauth.client"].create({
            "name": "Other", "redirect_uri_ids": [(0, 0, {"uri": "https://other.example/cb"})],
        })
        raw = self._issue_code()
        self.assertFalse(self.Code._consume(raw, other))
        # ...and remains usable by its rightful client.
        self.assertTrue(self.Code._consume(raw, self.client))

    def test_expired_code_is_rejected(self):
        from datetime import timedelta
        from odoo import fields as odoo_fields

        raw = self._issue_code()
        record = self.Code.search([], order="create_date desc", limit=1)
        record.expires_at = odoo_fields.Datetime.now() - timedelta(seconds=1)
        self.assertFalse(self.Code._consume(raw, self.client))

    def test_unknown_code_is_rejected(self):
        self.assertFalse(self.Code._consume("not-a-real-code", self.client))
        self.assertFalse(self.Code._consume("", self.client))

    # -- tokens ---------------------------------------------------------
    def test_access_and_refresh_are_separate_namespaces(self):
        access = self.Token._issue(self.client, self.user, "openid", token_type="access")
        refresh = self.Token._issue(self.client, self.user, "openid", token_type="refresh")
        self.assertNotEqual(access, refresh)
        # An access token must not be usable as a refresh token, or vice versa.
        self.assertFalse(self.Token._resolve(access, self.client, token_type="refresh"))
        self.assertFalse(self.Token._resolve(refresh, self.client, token_type="access"))
        self.assertTrue(self.Token._resolve(access, self.client, token_type="access"))
        self.assertTrue(self.Token._resolve(refresh, self.client, token_type="refresh"))

    def test_revoked_token_stops_resolving(self):
        raw = self.Token._issue(self.client, self.user, "openid", token_type="access")
        record = self.Token._resolve(raw, self.client, token_type="access")
        self.assertTrue(record)
        record.action_revoke()
        self.assertFalse(self.Token._resolve(raw, self.client, token_type="access"))

    def test_token_of_other_client_does_not_resolve(self):
        other = self.env["mint.oauth.client"].create({
            "name": "Other", "redirect_uri_ids": [(0, 0, {"uri": "https://other.example/cb"})],
        })
        raw = self.Token._issue(self.client, self.user, "openid", token_type="access")
        self.assertFalse(self.Token._resolve(raw, other, token_type="access"))

    # -- claims ----------------------------------------------------------
    def test_claims_follow_granted_scope_only(self):
        only_openid = MintOidcFlow._profile_claims(self.user, "openid")
        self.assertEqual(only_openid, {}, "no profile/email scope means no claims")

        with_email = MintOidcFlow._profile_claims(self.user, "openid email")
        self.assertEqual(with_email["email"], "designer@themintdispensary.com")
        self.assertNotIn("name", with_email)

        full = MintOidcFlow._profile_claims(self.user, "openid email profile")
        self.assertEqual(full["name"], "Designer")
        self.assertEqual(full["preferred_username"], self.user.login)

    # -- access group ----------------------------------------------------
    def test_group_membership_gates_access(self):
        self.assertTrue(self.user.has_group("mint_oauth_provider.group_oidc_user"))
        self.user.group_ids = [
            (3, self.env.ref("mint_oauth_provider.group_oidc_user").id)
        ]
        self.assertFalse(self.user.has_group("mint_oauth_provider.group_oidc_user"))

    def test_plain_internal_user_has_no_access(self):
        """Being an employee must not be sufficient."""
        plain = self.env["res.users"].create({
            "name": "Warehouse", "login": "warehouse@themintdispensary.com",
        })
        self.assertFalse(plain.has_group("mint_oauth_provider.group_oidc_user"))
