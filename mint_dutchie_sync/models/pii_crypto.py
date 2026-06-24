# -*- coding: utf-8 -*-
"""
Fernet (AES-128-CBC + HMAC-SHA256) encryption helpers for Dutchie customer PII.

The symmetric key resolves, in order:
  1. ``DUTCHIE_PII_FERNET_KEY`` environment variable (PRODUCTION — set on the
     Odoo Railway service; never stored in the database).
  2. ``mint_dutchie_sync.pii_fernet_key`` system parameter (non-prod / fallback).

Generate a key with::

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Encryption FAILS CLOSED: if no key is configured, ``encrypt_value`` raises rather
than silently persisting DOB / driver's-license / MJ-state-ID as plaintext.
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


def _get_cipher(env):
    """Return a Fernet cipher, or None if no usable key is configured."""
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


def encrypt_value(env, plaintext):
    """Encrypt ``plaintext`` to a Fernet token string. Empty -> False.

    Fails closed: raises ValueError when no key is configured so PII is never
    written to the database in the clear.
    """
    if not plaintext:
        return False
    cipher = _get_cipher(env)
    if cipher is None:
        raise ValueError(
            "Dutchie PII encryption key not configured (set the %s env var or "
            "the %s system parameter); refusing to store DOB/DL/MJ-ID "
            "unencrypted." % (ENV_KEY, PARAM_KEY)
        )
    return cipher.encrypt(str(plaintext).encode()).decode()


def decrypt_value(env, ciphertext):
    """Decrypt a Fernet token back to plaintext. Empty/undecryptable -> False."""
    if not ciphertext:
        return False
    cipher = _get_cipher(env)
    if cipher is None:
        return False
    try:
        return cipher.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        _logger.error("Failed to decrypt Dutchie PII value (wrong or rotated key?)")
        return False
