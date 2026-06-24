# -*- coding: utf-8 -*-
"""
Fernet (AES-128-CBC + HMAC-SHA256) encryption helpers for Dutchie customer PII.

The symmetric key resolves, in order:
  1. ``DUTCHIE_PII_FERNET_KEY`` environment variable — the PRODUCTION source.
     Set it on the Odoo Railway service so the key lives in the process
     environment, NOT in the database alongside the ciphertext it protects.
  2. ``mint_dutchie_sync.pii_fernet_key`` system parameter — DEV / NON-PROD
     fallback only. WARNING: this stores the key in ir_config_parameter, i.e.
     in the same Postgres DB as the encrypted columns, so a DB dump leaks both
     key and ciphertext. Do not rely on it in production — provision the env var.

Generate a key with::

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Encryption FAILS CLOSED: ``encrypt_value`` returns nothing usable without a key.
Callers in the ORM layer (see res_partner.py) raise a clean UserError rather
than persisting DOB / driver's-license / MJ-state-ID in the clear.
"""
import logging
import os

_logger = logging.getLogger(__name__)

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover - cryptography ships with Odoo's image
    Fernet = None
    InvalidToken = Exception
    _logger.warning(
        "cryptography library unavailable; Dutchie PII encryption is disabled"
    )

ENV_KEY = 'DUTCHIE_PII_FERNET_KEY'
PARAM_KEY = 'mint_dutchie_sync.pii_fernet_key'


def get_cipher(env):
    """Return a Fernet cipher, or None if no usable key is configured.

    Resolve ONCE per recordset and pass the result to encrypt_value/decrypt_value
    to avoid rebuilding the cipher (and re-reading the key) per record.
    """
    key = os.environ.get(ENV_KEY)
    if not key:
        key = env['ir.config_parameter'].sudo().get_param(PARAM_KEY)
    if not key or Fernet is None:
        return None
    if isinstance(key, str):
        key = key.encode()
    try:
        return Fernet(key)
    except Exception:  # malformed key
        _logger.error(
            "Invalid Fernet key in %s / %s; Dutchie PII encryption disabled",
            ENV_KEY, PARAM_KEY,
        )
        return None


def encrypt_value(env, plaintext, cipher=None):
    """Encrypt ``plaintext`` to a Fernet token string. Empty -> False.

    Fails closed: raises ValueError when no key is available. ORM callers pass a
    pre-resolved ``cipher`` and surface a UserError before reaching this guard.
    """
    if not plaintext:
        return False
    if cipher is None:
        cipher = get_cipher(env)
    if cipher is None:
        raise ValueError(
            "Dutchie PII encryption key not configured (set the %s env var or "
            "the %s system parameter); refusing to store DOB/DL/MJ-ID "
            "unencrypted." % (ENV_KEY, PARAM_KEY)
        )
    return cipher.encrypt(str(plaintext).encode()).decode()


def decrypt_value(env, ciphertext, cipher=None):
    """Decrypt a Fernet token back to plaintext. Empty/undecryptable -> False."""
    if not ciphertext:
        return False
    if cipher is None:
        cipher = get_cipher(env)
    if cipher is None:
        return False
    try:
        return cipher.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        _logger.error("Failed to decrypt Dutchie PII value (wrong or rotated key?)")
        return False
