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
import hashlib
import hmac
import logging
import os
import re

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


# ---------------------------------------------------------------------------
# Blind index
#
# Fernet is non-deterministic by design (random IV), so `x_dutchie_dl_enc`
# cannot be searched or joined. That is precisely why `x_dutchie_identity_key`
# still holds 1,661,769 licence numbers IN THE CLEAR: the welcome signup path
# has to match on them, and ciphertext offered no way to. The blind index
# below is the missing primitive — deterministic, keyed, one-way — so the
# join survives without the plaintext.
#
# ENTROPY WARNING. Licence numbers are low-entropy, and in Florida and
# Michigan they are derived algorithmically from name + DOB + gender, so
# candidates are computable rather than random. Therefore:
#   * anyone holding the pepper can brute-force the whole index offline;
#     the pepper must never sit in the database beside the column, and must
#     never reach the browser (which is why the raw DL still crosses TLS on
#     signup — client-side hashing would ship the pepper to every visitor);
#   * hashing does NOT stop enumeration. The per-IP throttle on the lookup
#     route remains the only bound on guessing, exactly as before.
# ---------------------------------------------------------------------------

#: Characters stripped before indexing. The legacy key is `'dl:' + DL.upper()`
#: with no further cleaning; this is deliberately MORE aggressive so the
#: 93,707 keys carrying a space or dash (5.6% of the corpus, measured
#: 2026-08-11) stop being unmatchable. The price is 891 pairs that collide
#: ONLY after punctuation is removed — those are flagged `x_dl_ambiguous` at
#: backfill and excluded from auto-match rather than silently merged into
#: one person.
_NON_ALNUM = re.compile(r'[^A-Z0-9]')

BLIND_INDEX_INFO = b'mint-blind-index-v1'


def normalize_dl(raw):
    """Normalise a licence number for indexing. Unusable input -> ''."""
    if not raw:
        return ''
    return _NON_ALNUM.sub('', str(raw).upper())


def _pepper(env):
    """Derive the HMAC pepper from the Fernet key via HKDF.

    Deriving rather than provisioning a second secret is deliberate: it means
    no new key to rotate, back up, or leak, and HKDF's `info` label keeps the
    two uses cryptographically separate so the index can never be used to
    attack the ciphertext (or vice versa).
    """
    key = os.environ.get(ENV_KEY) or env['ir.config_parameter'].sudo().get_param(PARAM_KEY)
    if not key:
        return None
    if isinstance(key, str):
        key = key.encode()
    return hmac.new(key, BLIND_INDEX_INFO, hashlib.sha256).digest()


def blind_index(env, value, domain='dl'):
    """Deterministic, keyed, one-way index for `value`. Unusable -> False.

    `domain` namespaces the input so the same string used as a licence and as
    something else cannot collide.

    Fails CLOSED: with no key configured this returns False rather than a
    bare hash. An unpeppered hash of a low-entropy licence number is
    reversible with a wordlist while still looking like it works, which is
    the kind of silence that ships.
    """
    norm = normalize_dl(value)
    if not norm:
        return False
    pepper = _pepper(env)
    if not pepper:
        _logger.error(
            'Blind index requested but no key configured (%s / %s); refusing '
            'to emit an unpeppered hash.', ENV_KEY, PARAM_KEY,
        )
        return False
    return hmac.new(pepper, ('%s:%s' % (domain, norm)).encode(), hashlib.sha256).hexdigest()
