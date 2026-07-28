# -*- coding: utf-8 -*-
"""RSA signing keys for the OIDC provider.

Relying parties (Penpot, and anything else we onboard later) verify our ID
tokens against the public half published at /oauth2/jwks. The private half
never leaves this table, is never rendered in a view, and is readable only
via sudo() from the token controller.

Key rotation is additive on purpose: `active` keys are all published in the
JWKS so tokens signed by an outgoing key keep verifying until they expire,
but only the `is_signing` key is used for new signatures. Retire an old key
by clearing `active` once its longest-lived token has expired.
"""
import base64
import logging
import secrets

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
except ImportError:  # pragma: no cover - the image pins cryptography==42.0.8
    serialization = None
    rsa = None

KEY_SIZE = 2048


def _b64u(data: bytes) -> str:
    """base64url without padding, per RFC 7515."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _int_to_b64u(value: int) -> str:
    length = (value.bit_length() + 7) // 8
    return _b64u(value.to_bytes(length, "big"))


class MintOauthKeypair(models.Model):
    _name = "mint.oauth.keypair"
    _description = "OIDC Signing Keypair"
    _order = "create_date desc"

    kid = fields.Char(
        string="Key ID",
        required=True,
        readonly=True,
        index=True,
        help="Published in the JWT header so verifiers pick the right JWKS entry.",
    )
    # groups= keeps the private key out of every view and out of read() for
    # non-system users, on top of the ir.model.access rules.
    private_pem = fields.Text(
        string="Private Key (PEM)",
        required=True,
        readonly=True,
        groups="base.group_system",
    )
    public_pem = fields.Text(string="Public Key (PEM)", required=True, readonly=True)
    is_signing = fields.Boolean(
        string="Active Signing Key",
        default=False,
        help="The one key used to sign new tokens. Exactly one should be set.",
    )
    active = fields.Boolean(
        default=True,
        help="Published in JWKS. Keep an outgoing key active until every token "
             "it signed has expired, otherwise verification breaks mid-session.",
    )

    _sql_constraints = [
        ("kid_uniq", "unique(kid)", "Key ID must be unique."),
    ]

    # ------------------------------------------------------------------
    @api.model
    def _generate(self, make_signing=True):
        """Create a new RSA keypair. Returns the record."""
        if rsa is None:
            raise UserError(
                "The 'cryptography' package is unavailable, so OIDC signing keys "
                "cannot be generated. It is pinned to 42.0.8 in the image."
            )
        key = rsa.generate_private_key(public_exponent=65537, key_size=KEY_SIZE)
        private_pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
        public_pem = key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()

        if make_signing:
            self.sudo().search([("is_signing", "=", True)]).write({"is_signing": False})

        record = self.sudo().create({
            "kid": secrets.token_hex(8),
            "private_pem": private_pem,
            "public_pem": public_pem,
            "is_signing": make_signing,
        })
        _logger.info("Generated OIDC signing keypair kid=%s", record.kid)
        return record

    @api.model
    def _get_signing_key(self):
        """The key new tokens are signed with, generating one on first use."""
        key = self.sudo().search([("is_signing", "=", True), ("active", "=", True)], limit=1)
        return key or self._generate()

    def _jwks(self):
        """This recordset rendered as a JWKS document."""
        keys = []
        for record in self:
            if not record.public_pem:
                continue
            public_key = serialization.load_pem_public_key(record.public_pem.encode())
            numbers = public_key.public_numbers()
            keys.append({
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": record.kid,
                "n": _int_to_b64u(numbers.n),
                "e": _int_to_b64u(numbers.e),
            })
        return {"keys": keys}

    def action_rotate(self):
        """Mint a fresh signing key, leaving this one active for verification."""
        self.ensure_one()
        return self._generate(make_signing=True)
