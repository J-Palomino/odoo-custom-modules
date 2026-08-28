# -*- coding: utf-8 -*-
"""
Strain master (`mint.strain`) — the curated list behind the Strain dropdown.

PHASE 1 OF TWO. This file deliberately does not touch `product.template`.

`product.template.strain` is free text with no dictionary behind it. Measured
on prod 2026-08-27: 70,280 of 84,488 templates populated, **4,800 distinct raw
values**, ~41% of them placeholders ("No Strain" alone is 23,997), and heavy
spelling drift — `Blue Dream` ships as six rows (`Blue Dream`, `BLUE DREAM`,
`Blue Dream (S)`, `(S/H)`, `(H)`, `(I)`). Nothing in the database could source
a strain dropdown: no strain model, and `product.attribute` is empty.

This model is that dictionary. It mirrors `mint.brand` on purpose — same
`aliases`-lines shape, same normalized resolver — so there is one pattern to
learn for both catalog masters.

**Why the link field is not here.** The first attempt (PR #360) added a stored
`product.template.strain_id` in the same release. The module upgrade did not
run on deploy, so the field existed in the registry with no DB column, and
because the ORM prefetches every stored field, *every* write touching
`categ_id` on product.template failed in production — the Dutchie sync
included. The feature was inert and still caused an outage. So phase 1 ships
only this model: if its upgrade silently skips, nothing references it and
nothing breaks. `strain_id`, the create/write sync and the product back-link
land in phase 2, gated on `ir.module.module.latest_version` actually reaching
19.0.4.28.0 — the version field, not a green deploy.

Consequently `product_count` here counts templates whose raw `strain` TEXT
resolves to a master, not rows joined by a foreign key. That is the number a
curator actually needs while triaging: how many products this entry would
claim. Phase 2 switches it to the real join.
"""
import logging
import re

from odoo import api, fields, models, tools

_logger = logging.getLogger(__name__)

# Raw values that occupy the strain column but do not name a strain. These
# never get a master record and never resolve to one — a product carrying one
# keeps its text and simply has no strain on file.
PLACEHOLDER_STRAINS = frozenset({
    'no strain', 'none', 'n/a', 'na', 'null', 'unknown', 'tbd', 'various',
    'non cannabis', 'noncannabis', 'not applicable',
    'hybrid', 'indica', 'sativa', 'cbd',
    'hybrid blend', 'indica blend', 'sativa blend', 'blend',
    'accessory', 'accessories', 'merch', 'merchandise',
})

# Trailing strain-type tags Dutchie/staff append to the name: "Blue Dream (S)",
# "Donny Burger (I)", "Cereal Milk (H)", "Blue Dream (S/H)", "Blue Zushi (h)".
_TYPE_SUFFIX_RE = re.compile(
    r'\s*\((?:s|i|h|s/h|h/s|i/h|h/i|cbd|sativa|indica|hybrid)\)\s*$',
    re.IGNORECASE,
)

_SUFFIX_TO_TYPE = {
    's': 'sativa', 'i': 'indica', 'h': 'hybrid',
    's/h': 'hybrid', 'h/s': 'hybrid', 'i/h': 'hybrid', 'h/i': 'hybrid',
    'cbd': 'cbd', 'sativa': 'sativa', 'indica': 'indica', 'hybrid': 'hybrid',
}


class MintStrain(models.Model):
    _name = "mint.strain"
    _description = "Cannabis Strain"
    _order = "product_count desc, name"

    name = fields.Char(string="Name", required=True, index=True)
    slug = fields.Char(string="URL Slug")
    strain_type = fields.Selection([
        ('sativa', 'Sativa'),
        ('indica', 'Indica'),
        ('hybrid', 'Hybrid'),
        ('cbd', 'CBD'),
    ], string="Strain Type")

    aliases = fields.Text(
        string="Aliases",
        help="Alternate spellings seen in the catalog, one per line "
             "(e.g. 'BLUE DREAM', 'Blue Dream (S)', 'Blue Dream (S/H)' all "
             "alias 'Blue Dream'). resolve_name() folds importer and staff "
             "free-text onto one master through these.",
    )

    lineage = fields.Char(string="Lineage", help="Parent cross, e.g. 'Blueberry x Haze'.")
    description = fields.Text(string="Description")
    effects = fields.Char(string="Effects (comma-separated)")
    flavors = fields.Char(string="Flavors (comma-separated)")

    active = fields.Boolean(
        string="Active",
        default=True,
        help="Archive instead of deleting. Archived strains drop out of the "
             "picker; product links (phase 2) survive.",
    )
    verified = fields.Boolean(
        string="Verified",
        default=False,
        help="Curator has reviewed this entry. Seeded rows land unverified — "
             "the seed cannot tell a real strain ('Fruit Punch') from a "
             "mis-typed descriptor ('Rolling Paper', 'Accessory').",
    )

    # Plain stored Integer, NOT a compute. It is derived from raw text on
    # product.template, which no ORM dependency can express; refreshed by
    # seed_from_products() and refresh_product_counts(). Stored so the list
    # view can order and the search view can filter on it.
    product_count = fields.Integer(
        string="# Products",
        default=0,
        readonly=True,
        help="Templates whose raw strain text resolves to this master. "
             "Refreshed by the seed, not live.",
    )

    # Odoo 19: `_sql_constraints` is silently ignored — must use models.Constraint.
    _name_unique = models.Constraint(
        'UNIQUE(name)',
        'Strain name must be unique!',
    )

    # ── normalization / resolution ──────────────────────────────────────────

    @staticmethod
    def _norm_strain_name(s):
        """Normalize a strain string for matching.

        Lowercase, drop a trailing type tag ("(S)", "(S/H)"), drop the trailing
        '*' merchandisers append, collapse punctuation and whitespace. Mirrors
        mint.brand._norm_brand_name in shape so both masters behave alike.
        """
        s = (s or '').strip().lower()
        s = _TYPE_SUFFIX_RE.sub('', s)
        s = s.rstrip('*').strip()
        s = re.sub(r'[^a-z0-9]+', ' ', s)
        return re.sub(r'\s+', ' ', s).strip()

    @staticmethod
    def _type_from_suffix(s):
        """strain_type implied by a trailing '(S)'/'(I)'/'(H)' tag, else None."""
        m = _TYPE_SUFFIX_RE.search((s or '').strip())
        if not m:
            return None
        tag = m.group(0).strip().strip('()').strip().lower()
        return _SUFFIX_TO_TYPE.get(tag)

    @classmethod
    def _is_placeholder(cls, s):
        """True when the raw value occupies the column without naming a strain."""
        n = cls._norm_strain_name(s)
        return (not n) or (n in PLACEHOLDER_STRAINS)

    @api.model
    def _build_name_index(self):
        """{normalized name or alias: strain id} over all strains, archived included."""
        index = {}
        for rec in self.with_context(active_test=False).search_read([], ['name', 'aliases']):
            n = self._norm_strain_name(rec['name'])
            if n and n not in index:
                index[n] = rec['id']
            for line in (rec['aliases'] or '').splitlines():
                a = self._norm_strain_name(line)
                if a and a not in index:
                    index[a] = rec['id']
        return index

    @api.model
    @tools.ormcache()
    def _name_index_cached(self):
        """Cached {normalized: id} index, invalidated by create/write/unlink.

        Callers must treat the result as read-only — seed_from_products mutates
        its own index, so it calls _build_name_index() directly instead.
        """
        return self._build_name_index()

    @api.model
    def resolve_name(self, raw_name):
        """Resolve a free-text strain string to a mint.strain record.

        'BLUE DREAM', 'Blue Dream (S)' and 'blue dream' all land on the same
        master. Returns an empty recordset for placeholders and unknown names;
        it never creates. Use _get_or_create() when a caller should mint one.
        """
        if not raw_name or self._is_placeholder(raw_name):
            return self.browse()
        hit = self._name_index_cached().get(self._norm_strain_name(raw_name))
        return self.browse(hit) if hit else self.browse()

    # Names, aliases and the archived flag all change resolution, so drop the
    # cached index on every mutation. Cheap: written rarely, read constantly.
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        self.env.registry.clear_cache()
        return records

    def write(self, vals):
        res = super().write(vals)
        self.env.registry.clear_cache()
        return res

    def unlink(self):
        res = super().unlink()
        self.env.registry.clear_cache()
        return res

    @api.model
    def _get_or_create(self, raw_name, strain_type=None):
        """Resolve raw_name, creating the master when it is new and non-placeholder."""
        if not raw_name or self._is_placeholder(raw_name):
            return self.browse()
        existing = self.resolve_name(raw_name)
        if existing:
            return existing
        display = _TYPE_SUFFIX_RE.sub('', (raw_name or '').strip()).rstrip('*').strip()
        vals = {
            'name': display or raw_name.strip(),
            'strain_type': strain_type or self._type_from_suffix(raw_name),
        }
        if display != (raw_name or '').strip():
            vals['aliases'] = raw_name.strip()
        return self.create(vals)

    # ── seeding ─────────────────────────────────────────────────────────────

    @api.model
    def seed_from_products(self, min_count=1):
        """Build the master list from the existing product.template.strain text.

        READ-ONLY with respect to product.template — phase 1 writes nothing
        there. Idempotent: re-running folds new raw spellings into existing
        masters as aliases rather than creating duplicates.

        min_count -- only mint a master for normalized values used by at least
                     this many templates. Default 1 (seed everything real);
                     raise it to skip the long tail of one-off typos.

        Returns a summary dict.
        """
        Product = self.env['product.template'].sudo()
        self.env.flush_all()

        # read_group with no limit — an earlier probe that passed limit=2000
        # silently truncated and hid "No Strain" (23,997 rows) from the top of
        # the distribution.
        groups = Product.with_context(active_test=False).read_group(
            [('strain', '!=', False), ('strain', '!=', '')],
            ['strain'], ['strain'], lazy=False, limit=None,
        )

        buckets = {}
        skipped_placeholder = 0
        for g in groups:
            raw, count = g['strain'], g['__count']
            if self._is_placeholder(raw):
                skipped_placeholder += count
                continue
            key = self._norm_strain_name(raw)
            b = buckets.setdefault(key, {'count': 0, 'variants': {}})
            b['count'] += count
            b['variants'][raw] = b['variants'].get(raw, 0) + count

        # Existing strain_type per raw value, fetched ONCE. Per-bucket lookups
        # would fire ~2.9k GROUP BYs against an unindexed 84k-row char column.
        type_by_raw = self._strain_type_distribution()

        index = self._build_name_index()
        created = aliased = skipped_thin = 0

        for key, bucket in buckets.items():
            if bucket['count'] < min_count:
                skipped_thin += 1
                continue

            # Canonical display name = most-used raw spelling minus its type
            # tag. Ties break alphabetically so the seed is deterministic
            # across runs and environments.
            variants = sorted(bucket['variants'].items(), key=lambda kv: (-kv[1], kv[0]))
            top_raw = variants[0][0]
            display = _TYPE_SUFFIX_RE.sub('', top_raw.strip()).rstrip('*').strip() or top_raw.strip()

            inferred = None
            for raw, _n in variants:
                inferred = self._type_from_suffix(raw)
                if inferred:
                    break
            if not inferred:
                tally = {}
                for raw, _n in variants:
                    for stype, cnt in type_by_raw.get(raw, {}).items():
                        tally[stype] = tally.get(stype, 0) + cnt
                inferred = max(tally, key=tally.get) if tally else None

            strain = self.browse(index[key]) if key in index else self.browse()
            if not strain:
                strain = self.create({
                    'name': display,
                    'strain_type': inferred,
                    'slug': re.sub(r'[^a-z0-9]+', '-', key).strip('-') or False,
                })
                index[key] = strain.id
                created += 1
            elif inferred and not strain.strain_type:
                strain.strain_type = inferred

            # Record every raw spelling that differs from the master name, so
            # the exact-text matching in refresh_product_counts (and phase 2's
            # back-link) can claim those products.
            #
            # Dedupe on the RAW string, not the normalized one. Every variant
            # in a bucket normalizes to the bucket key by construction, which
            # is also the normalized name — so a normalized check rejects all
            # of them and no alias is ever recorded. That bug shipped: it left
            # "Blue Dream" claiming 87 templates instead of ~607, because
            # "BLUE DREAM" and "Blue Dream (S)" were never written down.
            have = {l.strip() for l in (strain.aliases or '').splitlines() if l.strip()}
            have.add(strain.name)
            new_lines = []
            for raw, _n in variants:
                r = raw.strip()
                if r and r not in have:
                    new_lines.append(r)
                    have.add(r)
            if new_lines:
                strain.aliases = '\n'.join(
                    [l for l in (strain.aliases or '').splitlines() if l.strip()] + new_lines
                )
                aliased += len(new_lines)

        counted = self.refresh_product_counts()

        summary = {
            'raw_values': len(groups),
            'buckets': len(buckets),
            'created': created,
            'aliases_added': aliased,
            'skipped_thin': skipped_thin,
            'placeholder_rows_skipped': skipped_placeholder,
            'templates_matched': counted,
        }
        _logger.info("mint.strain.seed_from_products: %s", summary)
        return summary

    @api.model
    def _strain_type_distribution(self):
        """{raw strain text: {strain_type: count}} across the whole catalog.

        One grouped query, used to infer a master's strain_type when no variant
        carries a '(S)'/'(I)'/'(H)' tag (69% of buckets in the prod data).
        """
        self.env.cr.execute("""
            SELECT strain, strain_type, COUNT(*)
              FROM product_template
             WHERE strain IS NOT NULL AND strain <> ''
               AND strain_type IS NOT NULL
             GROUP BY strain, strain_type
        """)
        dist = {}
        for raw, stype, count in self.env.cr.fetchall():
            dist.setdefault(raw, {})[stype] = count
        return dist

    @api.model
    def refresh_product_counts(self):
        """Recount, per master, the templates whose raw strain text matches it.

        Phase 1 has no foreign key to join on, so this matches `strain` against
        each master's name and alias lines. One temp map + one grouped query
        rather than a statement per strain: `strain` is an unindexed char
        column on 84k rows.

        Returns the total number of templates matched by any master.
        """
        # raw text -> strain id, first writer wins (name beats alias), matching
        # _build_name_index so the counts agree with what resolve_name() does.
        owner = {}
        strains = self.with_context(active_test=False).search([])
        for strain in strains:
            if strain.name and strain.name not in owner:
                owner[strain.name] = strain.id
        for strain in strains:
            for line in (strain.aliases or '').splitlines():
                alias = line.strip()
                if alias and alias not in owner:
                    owner[alias] = strain.id

        cr = self.env.cr
        self.env.flush_all()
        cr.execute("UPDATE mint_strain SET product_count = 0 WHERE product_count <> 0")
        if not owner:
            self.env.invalidate_all()
            return 0

        from psycopg2.extras import execute_values
        cr.execute("DROP TABLE IF EXISTS mint_strain_map")
        cr.execute("""
            CREATE TEMP TABLE mint_strain_map (raw_name VARCHAR, strain_id INTEGER)
            ON COMMIT DROP
        """)
        execute_values(
            cr._obj,
            "INSERT INTO mint_strain_map (raw_name, strain_id) VALUES %s",
            list(owner.items()),
            page_size=1000,
        )
        cr.execute("CREATE INDEX ON mint_strain_map (raw_name)")
        cr.execute("""
            UPDATE mint_strain s
               SET product_count = c.cnt
              FROM (
                    SELECT m.strain_id, COUNT(*) AS cnt
                      FROM product_template p
                      JOIN mint_strain_map m ON p.strain = m.raw_name
                     GROUP BY m.strain_id
              ) c
             WHERE s.id = c.strain_id
        """)
        cr.execute("SELECT COALESCE(SUM(product_count), 0) FROM mint_strain")
        total = cr.fetchone()[0]

        # mint_strain was changed behind the ORM's back.
        self.env.invalidate_all()
        return total
