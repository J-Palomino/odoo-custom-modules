# -*- coding: utf-8 -*-
"""Signing-key and JWKS tests.

The JWKS document is the only thing standing between us and a forged ID token
being accepted, and its base64url/bignum encoding is easy to get subtly wrong
in a way that still *looks* fine. These tests verify a real RS256 signature
against a key reconstructed purely from the published JWKS — the exact path a
relying party takes.
"""
import json
import time

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "mint_oauth")
class TestOauthKeypair(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Keypair = self.env["mint.oauth.keypair"]

    def test_generate_sets_single_signing_key(self):
        first = self.Keypair._generate()
        self.assertTrue(first.is_signing)
        self.assertTrue(first.private_pem.startswith("-----BEGIN PRIVATE KEY"))
        self.assertTrue(first.public_pem.startswith("-----BEGIN PUBLIC KEY"))

        second = self.Keypair._generate()
        first.invalidate_recordset()
        # Rotating must demote the previous key, never leave two signing keys.
        self.assertFalse(first.is_signing)
        self.assertTrue(second.is_signing)
        # ...but the old key stays published so its tokens still verify.
        self.assertTrue(first.active)

    def test_get_signing_key_bootstraps(self):
        self.Keypair.search([]).write({"active": False, "is_signing": False})
        key = self.Keypair._get_signing_key()
        self.assertTrue(key.is_signing)
        self.assertTrue(key.active)

    def test_jwks_encoding(self):
        key = self.Keypair._generate()
        doc = key._jwks()
        self.assertEqual(len(doc["keys"]), 1)
        jwk = doc["keys"][0]

        self.assertEqual(jwk["kty"], "RSA")
        self.assertEqual(jwk["alg"], "RS256")
        self.assertEqual(jwk["use"], "sig")
        self.assertEqual(jwk["kid"], key.kid)
        # 65537 is the exponent we generate with.
        self.assertEqual(jwk["e"], "AQAB")
        # RFC 7515 base64url: no padding, URL-safe alphabet.
        for part in (jwk["n"], jwk["e"]):
            self.assertNotIn("=", part)
            self.assertNotIn("+", part)
            self.assertNotIn("/", part)
        # 2048-bit modulus -> 256 bytes -> 342 unpadded base64 chars.
        self.assertEqual(len(jwk["n"]), 342)

    def test_signature_verifies_against_published_jwks(self):
        """The full relying-party path: sign here, verify from JWKS alone."""
        import jwt

        key = self.Keypair._generate()
        jwk = key._jwks()["keys"][0]
        now = int(time.time())
        claims = {
            "iss": "https://letsgomint.us", "sub": "42", "aud": "penpot",
            "exp": now + 300, "iat": now, "nonce": "n-0S6_WzA2Mj",
        }
        token = jwt.encode(
            claims, key.private_pem, algorithm="RS256", headers={"kid": key.kid}
        )
        self.assertEqual(jwt.get_unverified_header(token)["kid"], key.kid)

        public = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))
        decoded = jwt.decode(
            token, public, algorithms=["RS256"],
            audience="penpot", issuer="https://letsgomint.us",
        )
        self.assertEqual(decoded["sub"], "42")
        self.assertEqual(decoded["nonce"], "n-0S6_WzA2Mj")

    def test_token_from_other_key_is_rejected(self):
        """A token signed by a key we did not publish must not verify."""
        import jwt

        ours = self.Keypair._generate()
        theirs = self.Keypair._generate(make_signing=False)
        now = int(time.time())
        forged = jwt.encode(
            {"iss": "https://letsgomint.us", "sub": "42", "aud": "penpot",
             "exp": now + 300},
            theirs.private_pem, algorithm="RS256",
        )
        public = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(ours._jwks()["keys"][0]))
        with self.assertRaises(jwt.InvalidSignatureError):
            jwt.decode(forged, public, algorithms=["RS256"],
                       audience="penpot", issuer="https://letsgomint.us")

    def test_expired_token_is_rejected(self):
        import jwt

        key = self.Keypair._generate()
        now = int(time.time())
        stale = jwt.encode(
            {"iss": "https://letsgomint.us", "sub": "42", "aud": "penpot",
             "exp": now - 10, "iat": now - 600},
            key.private_pem, algorithm="RS256",
        )
        public = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(key._jwks()["keys"][0]))
        with self.assertRaises(jwt.ExpiredSignatureError):
            jwt.decode(stale, public, algorithms=["RS256"],
                       audience="penpot", issuer="https://letsgomint.us")
