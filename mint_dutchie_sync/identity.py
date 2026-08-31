# -*- coding: utf-8 -*-
"""The one place that decides what an identity key looks like.

``x_dutchie_identity_key`` is how a web signup finds the POS record for the same
human. Two code paths build it — the roster import here in mint_dutchie_sync,
and the web signup in mint_customer_api — and they were kept in agreement by a
comment saying "MUST stay byte-identical". That is not a mechanism: a normalisation
that drifts does not fail loudly, it silently matches nothing, and the 1.79M keys
already stored become unreachable for whichever path changed.

The two callers cannot share one function — they take different inputs (a roster
CSV row versus a signup payload, and the signup path holds a date object it must
render as MM/DD/YYYY). What they can share is this: the definition of the key
format itself. Each caller adapts its own input; nobody re-implements the format.

Deliberately free of Odoo imports so it can be tested without an Odoo instance,
which is what makes the drift test cheap enough to run everywhere.

PRECEDENCE — DL > MJ state id > Name+DOB > phone. Do not reorder: the stored keys
were generated in this order, so a row that has both a DL and a phone is keyed on
its DL, and looking it up by phone would miss.
"""

DL_PREFIX = 'dl:'
MJ_PREFIX = 'mj:'
NAME_DOB_PREFIX = 'nd:'
PHONE_PREFIX = 'ph:'


def _clean(value):
    """Trim, tolerating None and non-strings from CSV rows and JSON bodies."""
    if value is None:
        return ''
    return str(value).strip()


def dl_key(dl):
    """``dl:<LICENCE UPPERCASED>`` — the strongest key we hold."""
    dl = _clean(dl)
    return DL_PREFIX + dl.upper() if dl else None


def mj_key(mj_state_id):
    """``mj:<MJ STATE ID UPPERCASED>`` — medical patients without a licence."""
    mj = _clean(mj_state_id)
    return MJ_PREFIX + mj.upper() if mj else None


def name_dob_key(name, dob):
    """``nd:<NAME UPPERCASED>|<MM/DD/YYYY>``.

    The date format is not cosmetic. The roster — and therefore every stored
    ``nd:`` key — uses MM/DD/YYYY, while the ID scanner posts ISO yyyy-mm-dd.
    Passing an ISO string through here builds a key that matches no row, so
    callers holding a date object must render it before calling.
    """
    nm = _clean(name).upper()
    d = _clean(dob)
    if not nm or not d:
        return None
    return '%s%s|%s' % (NAME_DOB_PREFIX, nm, d)


def phone_key(phone):
    """``ph:<PHONE>`` — last resort, and deliberately NOT normalised.

    Stored keys were built from the raw roster string, so stripping punctuation
    here would stop matching them. Weak by nature: junk numbers like
    ``0000000000`` are real rows in the roster and collide across many people.
    """
    ph = _clean(phone)
    return PHONE_PREFIX + ph if ph else None


def identity_key(dl=None, mj_state_id=None, name=None, dob=None, phone=None):
    """Best available key for one person, in precedence order.

    Returns None when nothing stable is available — the caller should skip the
    row rather than invent a key, because a non-deterministic key is worse than
    no key: it creates a partner that can never be matched again.
    """
    return (
        dl_key(dl)
        or mj_key(mj_state_id)
        or name_dob_key(name, dob)
        or phone_key(phone)
    )
