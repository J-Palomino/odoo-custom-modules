# -*- coding: utf-8 -*-
"""Canonical Dutchie <-> Odoo <-> Redis discount mapping (Odoo side).

The ONE source of truth for calculation-method, threshold-type, and
application-method codes, backed by ../data/discount-canonical.json — kept
byte-identical to the copy in the letsgomint-us repo
(docs/mappings/discount-canonical.json + the mintinvsvc in-package copy),
enforced by drift-guard tests on both sides.

Replaces the maps that had DRIFTED and INVERTED before 2026-06-05:
  - mint_redis_push CALC_METHOD_ID_TO_STRING  (was 1->PERCENT_OFF, 2->FIXED..., 15->PRICE_TO_AMOUNT_TOTAL)
  - mint_command_center CALC_METHOD_ID_FALLBACK (was percent->1, fixed->2)
Live-verified truth: 1=FLAT_AMOUNT_OFF, 2=PERCENT_OFF, 15=LOYALTY, and the
value for ids 1/5/15 lives in discount_value (not discount_amount).

This module holds NO Odoo model — it's a plain constants/helpers module so it
can be imported from any model mixin without import-order concerns.
"""
import json
import logging
import os

_logger = logging.getLogger(__name__)

_JSON_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'discount-canonical.json')

with open(_JSON_PATH, 'r') as _fh:
    REGISTRY = json.load(_fh)

# ── Calculation method ──────────────────────────────────────────────────────
CALC_METHOD_BY_ID = {}        # 2 -> 'PERCENT_OFF'
CALC_METHOD_TO_ID = {}        # 'PERCENT_OFF' -> 2  (+ odooType aliases)
VALUE_FIELD_BY_ID = {}        # 1 -> 'discount_value'
ODOO_TYPE_BY_ID = {}          # 1 -> 'fixed'
ODOO_TYPE_TO_ID = {}          # 'fixed' -> 1
CANONICAL_TO_ODOO_TYPE = {}   # 'FLAT_AMOUNT_OFF' -> 'fixed'

for _m in REGISTRY['calcMethods']:
    CALC_METHOD_BY_ID[_m['id']] = _m['canonical']
    CALC_METHOD_TO_ID[_m['canonical']] = _m['id']
    CALC_METHOD_TO_ID[_m['odooType']] = _m['id']
    VALUE_FIELD_BY_ID[_m['id']] = _m['valueField']
    ODOO_TYPE_BY_ID[_m['id']] = _m['odooType']
    ODOO_TYPE_TO_ID[_m['odooType']] = _m['id']
    CANONICAL_TO_ODOO_TYPE[_m['canonical']] = _m['odooType']

# ── Threshold type ──────────────────────────────────────────────────────────
THRESHOLD_TYPE_ID_BY_ODOO = {}   # 'items' -> 1
THRESHOLD_ODOO_BY_ID = {}        # 1 -> 'items'
for _t in REGISTRY['thresholdTypes']:
    THRESHOLD_TYPE_ID_BY_ODOO[_t['odooType']] = _t['id']
    THRESHOLD_ODOO_BY_ID[_t['id']] = _t['odooType']

# ── Application method ──────────────────────────────────────────────────────
APP_METHOD_ID_BY_ODOO = {}       # 'automatic' -> 1
for _a in REGISTRY['appMethods']:
    APP_METHOD_ID_BY_ODOO[_a['odooType']] = _a['id']


def calc_method_id_for(discount):
    """Authoritative Dutchie CalculationMethodId for a mint.discount record.

    Prefer the stored integer calculation_method_id (set by the Dutchie read
    sync). Fall back to the odoo discount_type only when the integer is unset
    (e.g. hand-authored PTL deals). Returns None when neither resolves so the
    caller can warn rather than mislabel."""
    cmid = getattr(discount, 'calculation_method_id', 0) or 0
    if cmid and cmid in CALC_METHOD_BY_ID:
        return int(cmid)
    dt = getattr(discount, 'discount_type', None)
    return ODOO_TYPE_TO_ID.get(dt)


def value_field_for_id(cmid):
    """Which mint.discount field holds the numeric value for this calc method:
    'discount_amount' for percent/price types, 'discount_value' for the
    $-off / order-$-off / loyalty types whose discount_amount is 0."""
    return VALUE_FIELD_BY_ID.get(int(cmid) if cmid else 0, 'discount_amount')


def discount_value_for(discount):
    """Read the numeric value off whichever field the calc method stores it in.
    This is the fix for ids 1/5/15, where the value lives in discount_value
    and discount_amount is 0 — every old emitter read discount_amount and got 0."""
    cmid = calc_method_id_for(discount)
    field = value_field_for_id(cmid) if cmid else 'discount_amount'
    val = getattr(discount, field, 0.0) or 0.0
    # Defensive: if the canonical field is empty but the other holds a value,
    # fall back so a half-populated row still emits something usable.
    if not val:
        other = 'discount_value' if field == 'discount_amount' else 'discount_amount'
        val = getattr(discount, other, 0.0) or 0.0
    return val


def calc_method_string_for(discount, warn=True):
    """Canonical calc-method string (PERCENT_OFF, ...) for a record, or
    'PERCENT_OFF' as a safe no-op default (percent of a 0 amount = no change)
    with a warning when the id is unknown."""
    cmid = calc_method_id_for(discount)
    s = CALC_METHOD_BY_ID.get(cmid) if cmid else None
    if not s:
        if warn:
            _logger.warning(
                'discount_canonical: unresolved calc method for discount %s '
                '(calculation_method_id=%s, discount_type=%s) — defaulting PERCENT_OFF',
                getattr(discount, 'id', '?'),
                getattr(discount, 'calculation_method_id', None),
                getattr(discount, 'discount_type', None),
            )
        return 'PERCENT_OFF'
    return s


def odoo_type_for_id(cmid):
    """Authoritative mint.discount.discount_type selection value for a Dutchie
    CalculationMethodId. Used to de-mislabel discount_type from the (correct)
    calculation_method_id."""
    return ODOO_TYPE_BY_ID.get(int(cmid)) if cmid else None


def parse_raw_restriction(raw):
    """Parse a *_restriction_ids_raw Char ('include: 62375 | 62376' or
    'exclude: 54') back to canonical {'ids': [...], 'isExclusion': bool}.
    Returns None when empty. Dutchie IDs stay int when they parse as int.

    Shared by BOTH the Redis serializer (mint_redis_push) and the Dutchie push
    (mint_command_center) so source=dutchie rows — whose m2m FKs are empty and
    whose Dutchie IDs live only in the raw Char field — resolve their targeting
    on both paths. Lives here because mint_redis_push depends on
    mint_command_center, so a helper shared by both can't live in either."""
    if not raw:
        return None
    s = raw.strip()
    is_exclusion = s.lower().startswith('exclude:')
    if ':' in s:
        s = s.split(':', 1)[1].strip()
    parts = [p.strip() for p in s.split('|') if p.strip()]
    if not parts:
        return None
    ids = []
    for p in parts:
        try:
            ids.append(int(p))
        except ValueError:
            ids.append(p)
    return {'ids': ids, 'isExclusion': is_exclusion}


def format_dutchie_date(d):
    """Format a date/datetime as Dutchie's update-discount-item expects
    ('M/D/YYYY, h:mm:ss A'). Empty string for falsy input. Mirrors the JS
    toDutchieWrite date rule."""
    if not d:
        return ''
    return '%d/%d/%d, 12:00:00 AM' % (d.month, d.day, d.year)
