"""
Pre-migration for 19.0.4.5.6 — convert mint_ptl_deal.product_category from
free-text Char to Selection (Flower / Pre-Rolls / Vapes / Edibles & Tinctures /
Concentrates & Topicals / Featured Deals).

Steps:
  1. Add product_category_legacy column (audit preservation).
  2. Copy current product_category into product_category_legacy.
  3. Heuristic-map legacy values into one of the 6 master buckets.
  4. NULL out values that don't match any bucket — operator will re-pick in UI.

Idempotent: re-running won't re-overwrite legacy if it's already populated.
"""
import logging
import re

_logger = logging.getLogger(__name__)


# Ordered priority — first matching pattern wins. Pre-Rolls must come before
# Flower (a "preroll" contains "roll" but isn't bulk flower); Concentrates
# must come before Vapes for "Live Rosin"/"Live Resin" plain concentrate
# entries (we keep Vapes ahead since the legacy free-text mostly described
# carts/AIOs); Edibles last among ambiguous food-like terms.
BUCKET_PATTERNS = [
    ('Pre-Rolls', [
        r'pre[\s-]?roll', r'\bblunt\b', r'\bjoint\b',
        r'infused\s*pre', r'hash\s*hole', r'\binfused\b.*\bpr',
    ]),
    ('Vapes', [
        r'\baio\b', r'cartridge', r'\bcart\b', r'disposable',
        r'\bvape\b', r'distillate', r'live[\s-]?resin', r'live[\s-]?rosin',
        r'\bpod\b', r'\bdab\s*pen\b',
    ]),
    ('Concentrates & Topicals', [
        r'concentrate', r'\brosin\b', r'\bhash\b', r'\bwax\b',
        r'shatter', r'diamond', r'crumble', r'topical', r'salve',
        r'\brso\b', r'\bfso\b', r'live\s*sugar', r'badder',
        r'\bsauce\b', r'\bccg\b',
    ]),
    ('Edibles & Tinctures', [
        r'gumm', r'chocolat', r'edible', r'beverage', r'syrup',
        r'tincture', r'capsule', r'\bdrink\b', r'cookie', r'brownie',
        r'sucker', r'\bchew', r'lozenge', r'tablet', r'\bmg\b',
        r'\b\d+\s*mg', r'\bbrix\b', r'\bbelt', r'\brings?\b',
    ]),
    ('Flower', [
        r'\bflower\b', r'\bshake\b', r'\btrim\b', r'\bbulk\b',
        r'\bprepack\b', r'\beighth\b', r'\b1\/8\b', r'\boz\b',
    ]),
    ('Featured Deals', [
        r'featured', r'special\s*offer', r'edlp',
    ]),
]


def _classify(raw):
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    # Exact-match canonical (case-insensitive) — short-circuit
    canonical = {
        'flower': 'Flower',
        'pre-rolls': 'Pre-Rolls', 'pre rolls': 'Pre-Rolls', 'prerolls': 'Pre-Rolls',
        'vapes': 'Vapes', 'vape': 'Vapes',
        'edibles & tinctures': 'Edibles & Tinctures', 'edibles': 'Edibles & Tinctures',
        'concentrates & topicals': 'Concentrates & Topicals', 'concentrates': 'Concentrates & Topicals',
        'featured deals': 'Featured Deals', 'featured': 'Featured Deals',
    }
    lowered = text.lower()
    if lowered in canonical:
        return canonical[lowered]
    for bucket, patterns in BUCKET_PATTERNS:
        for pat in patterns:
            if re.search(pat, text, re.IGNORECASE):
                return bucket
    return None


def migrate(cr, version):
    if not version:
        return

    # 1. Ensure legacy column exists.
    cr.execute("""
        ALTER TABLE mint_ptl_deal
        ADD COLUMN IF NOT EXISTS product_category_legacy VARCHAR
    """)

    # 2. Copy current product_category to legacy where legacy is empty.
    cr.execute("""
        UPDATE mint_ptl_deal
        SET product_category_legacy = product_category
        WHERE product_category IS NOT NULL
          AND product_category <> ''
          AND (product_category_legacy IS NULL OR product_category_legacy = '')
    """)
    cr.execute("SELECT COUNT(*) FROM mint_ptl_deal WHERE product_category_legacy IS NOT NULL")
    legacy_count = cr.fetchone()[0]
    _logger.info('[19.0.4.5.6] preserved %d product_category values to product_category_legacy', legacy_count)

    # 3. Classify each distinct legacy value into one of the 6 buckets.
    cr.execute("""
        SELECT DISTINCT product_category_legacy
        FROM mint_ptl_deal
        WHERE product_category_legacy IS NOT NULL
          AND product_category_legacy <> ''
    """)
    mapped = 0
    unmapped = 0
    for (legacy_value,) in cr.fetchall():
        bucket = _classify(legacy_value)
        if bucket:
            cr.execute(
                "UPDATE mint_ptl_deal SET product_category = %s WHERE product_category_legacy = %s",
                (bucket, legacy_value),
            )
            mapped += cr.rowcount
        else:
            cr.execute(
                "UPDATE mint_ptl_deal SET product_category = NULL WHERE product_category_legacy = %s",
                (legacy_value,),
            )
            unmapped += cr.rowcount
    _logger.info(
        '[19.0.4.5.6] product_category mapped=%d unmapped=%d (legacy preserved)',
        mapped, unmapped,
    )
