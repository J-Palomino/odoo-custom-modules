# -*- coding: utf-8 -*-
"""Client credential and redirect-URI tests.

Loose redirect-URI matching is the classic OAuth account-takeover vector, so
the strictness of `_redirect_uri_allowed` is asserted explicitly rather than
left to review.
"""
from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "mint_oauth")
class TestOauthClient(TransactionCase):

    def setUp(self):
        super().setUp()
        self.client = self.env["mint.oauth.client"].create({
            "name": "Penpot",
            "redirect_uri_ids": [(0, 0, {
                "uri": "https://ux.letsgomint.us/api/auth/oauth/oidc/callback",
            })],
        })

    def test_secret_is_stored_hashed_and_verifies(self):
        raw = self.client._new_secret()
        self.assertTrue(raw)
        # The plaintext must never be what we persist.
        self.assertNotEqual(self.client.client_secret_hash, raw)
        self.assertTrue(self.client._verify_secret(raw))
        self.assertFalse(self.client._verify_secret(raw + "x"))
        self.assertFalse(self.client._verify_secret(""))
        self.assertFalse(self.client._verify_secret(None))

    def test_regenerating_invalidates_previous_secret(self):
        old = self.client._new_secret()
        new = self.client._new_secret()
        self.assertNotEqual(old, new)
        self.assertFalse(self.client._verify_secret(old))
        self.assertTrue(self.client._verify_secret(new))

    def test_redirect_uri_is_matched_exactly(self):
        good = "https://ux.letsgomint.us/api/auth/oauth/oidc/callback"
        self.assertTrue(self.client._redirect_uri_allowed(good))

        # Every one of these is a real-world takeover attempt shape.
        for bad in (
            "https://ux.letsgomint.us/api/auth/oauth/oidc/callback/../../evil",
            "https://ux.letsgomint.us/api/auth/oauth/oidc/callback?next=//evil.com",
            "https://ux.letsgomint.us/api/auth/oauth/oidc/callbackX",
            "https://evil.com/api/auth/oauth/oidc/callback",
            "https://ux.letsgomint.us.evil.com/api/auth/oauth/oidc/callback",
            "http://ux.letsgomint.us/api/auth/oauth/oidc/callback",
            "https://ux.letsgomint.us/",
            "", None,
        ):
            self.assertFalse(
                self.client._redirect_uri_allowed(bad),
                "redirect URI should have been rejected: %r" % (bad,),
            )

    def test_redirect_uri_must_be_https(self):
        with self.assertRaises(ValidationError):
            self.client.write({"redirect_uri_ids": [(0, 0, {
                "uri": "http://evil.com/callback",
            })]})

    def test_redirect_uri_rejects_fragment(self):
        with self.assertRaises(ValidationError):
            self.client.write({"redirect_uri_ids": [(0, 0, {
                "uri": "https://ux.letsgomint.us/cb#frag",
            })]})

    def test_localhost_http_allowed_for_dev(self):
        self.client.write({"redirect_uri_ids": [(0, 0, {
            "uri": "http://localhost:8899/callback",
        })]})
        self.assertTrue(self.client._redirect_uri_allowed("http://localhost:8899/callback"))

    def test_openid_scope_is_required(self):
        with self.assertRaises(ValidationError):
            self.client.write({"scopes": "profile email"})

    def test_find_active_skips_archived(self):
        Client = self.env["mint.oauth.client"]
        self.assertEqual(Client._find_active(self.client.client_id), self.client)
        self.client.active = False
        self.assertFalse(Client._find_active(self.client.client_id))
        self.assertFalse(Client._find_active("does-not-exist"))
        self.assertFalse(Client._find_active(None))
