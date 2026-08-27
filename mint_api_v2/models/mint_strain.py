# -*- coding: utf-8 -*-
"""
Strain master (`mint.strain`) — the curated list behind the Strain dropdown.

Before this model, `product.template.strain` was a free-text Char written by
the Dutchie importer and by staff. Measured on prod 2026-08-27: 70,280 of
84,488 templates populated, **4,800 distinct raw values**, of which ~41% are
placeholders ("No Strain" 23,997, "Hybrid (H)", "NON CANNABIS", "Hybrid
Blend", "Indica (I)", "Sativa (S)"). The rest carry heavy spelling drift —
`Blue Dream` / `BLUE DREAM` / `Blue Dream (S)` / `Blue Dream (S/H)` are four
rows for one strain. There was no dictionary anywhere: no strain model, and
`product.attribute` is empty in this DB.

This model is the dictionary. It mirrors `mint.brand` deliberately — same
`aliases`-lines shape, same normalized resolver, same stored `product_count`
gate for domain-filtered dropdowns — so there is one pattern to learn for
both catalog masters.

`product.template.strain` (Char) is NOT removed. The inventory service reads
it (`packages/inventory-service/services/cacheSync.js:94` merges `strain`)
and the storefront types it (`src/lib/redis-api.ts:71`), so it stays as the
denormalized mirror and is kept in sync from `strain_id` on write.
"""
import logging
import re

from psycopg2.extras import execute_values

from odoo import api, fields, models, tools

_logger = logging.getLogger(__name__)

# Raw values that occupy the strain column but do not name a strain. These are
# never given a master record and never resolve to one — a product carrying
# one of these keeps its legacy text and gets a NULL strain_id, which is the
# honest representation of "no strain on file".
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

# The suffix tag itself, mapped to the strain_type selection value.
_SUFFIX_TO_TYPE = {
    's': 'sativa',
    'i': 'indica',
    'h': 'hybrid',
    's/h': 'hybrid',
    'h/s': 'hybrid',
    'i/h': 'hybrid',
    'h/i': 'hybrid',
    'cbd': 'cbd',
    'sativa': 'sativa',
    'indica': 'indica',
    'hybrid': 'hybrid',
}


class MintStrain(models.Model):
    _name = "mint.strain"
    _description = "Cannabis Strain"
    _order = "product_count desc, name"
    _rec_name = "name"

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
             "alias 'Blue Dream'). Used by resolve_name() to fold importer "
             "and staff free-text onto one master record.",
    )

    lineage = fields.Char(string="Lineage", help="Parent cross, e.g. 'Blueberry x Haze'.")
    description = fields.Text(string="Description")
    effects = fields.Char(string="Effects (comma-separated)")
    flavors = fields.Char(string="Flavors (comma-separated)")

    active = fields.Boolean(
        string="Active",
        default=True,
        help="Archive instead of deleting. Archived strains drop out of the "
             "product dropdown but existing product links survive.",
    )
    verified = fields.Boolean(
        string="Verified",
        default=False,
        help="Curator has reviewed this entry. Seeded rows land unverified — "
             "the seed cannot tell a real strain ('Fruit Punch') from a "
             "mis-typed flavor or SKU descriptor ('Rolling Paper').",
    )

    product_ids = fields.One2many(
        'product.template', 'strain_id', string="Products",
    )

    # Stored so domains can filter on it (same rationale as mint.brand.product_count).
    # Recomputed by seed_from_products() and by product.template create/write.
    product_count = fields.Integer(
        string="# Products",
        compute='_compute_product_count',
        store=True,
        help="Count of product.template records linked to this strain.",
    )

    # Odoo 19: `_sql_constraints` is silently ignored — must use models.Constraint.
    _name_unique = models.Constraint(
        'UNIQUE(name)',
        'Strain name must be unique!',
    )

    @api.depends('name')  # trivial dep — real recompute is driven from product.template
    def _compute_product_count(self):
        Product = self.env['product.template'].sudo()
        for strain in self:
            strain.product_count = Product.search_count([('strain_id', '=', strain.id)])

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
        """Return the strain_type implied by a trailing '(S)'/'(I)'/'(H)' tag, or None."""
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
        """Cached {normalized: id} index.

        The Dutchie importer writes `strain` on tens of thousands of templates
        per run and each write resolves; rebuilding a ~4k-row index every time
        would dominate the import. Invalidated by create/write/unlink below.
        Callers must treat the returned dict as read-only — seed_from_products
        mutates its index, so it calls _build_name_index() directly instead.
        """
        return self._build_name_index()

    @api.model
    def resolve_name(self, raw_name):
        """Resolve a free-text strain string to a mint.strain record.

        Matches on normalized name and on alias lines, so 'BLUE DREAM',
        'Blue Dream (S)' and 'blue dream' all land on the same master.
        Returns an empty recordset for placeholders and unknown names — it
        never creates. Use _get_or_create() when a write should mint a master.
        """
        if not raw_name or self._is_placeholder(raw_name):
            return self.browse()
        hit = self._name_index_cached().get(self._norm_strain_name(raw_name))
        return self.browse(hit) if hit else self.browse()

    # Any change to names, aliases or the archived flag changes resolution, so
    # drop the cached index on every mutation. Cheap: mint.strain is written
    # rarely (seed runs, curator edits), read constantly.
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
        # Store the display form without the type tag; keep the raw as an alias
        # so the next importer pass resolves instead of creating a duplicate.
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
    def seed_from_products(self, min_count=1, link=True):
        """Build the master list from the existing product.template.strain text.

        Idempotent: re-running folds new raw spellings into existing masters as
        aliases rather than creating duplicates, so it is safe to call from a
        migration and again by hand after an importer run.

        min_count -- only mint a master for normalized values used by at least
                     this many templates. Default 1 (seed everything real);
                     raise it to skip the long tail of one-off typos.
        link      -- also back-fill product.template.strain_id.

        Returns a dict summary for the migration log.
        """
        Product = self.env['product.template'].sudo()

        # _strain_type_distribution reads product_template with raw SQL, so any
        # pending ORM writes must be on disk first.
        self.env.flush_all()

        # Group the raw column. read_group with no limit — an earlier probe that
        # passed limit=2000 silently truncated and hid "No Strain" (23,997 rows)
        # from the top of the distribution.
        groups = Product.with_context(active_test=False).read_group(
            [('strain', '!=', False), ('strain', '!=', '')],
            ['strain'], ['strain'], lazy=False, limit=None,
        )

        # normalized key -> {'count', 'variants': {raw: count}}
        buckets = {}
        skipped_placeholder = 0
        for g in groups:
            raw = g['strain']
            count = g['__count']
            if self._is_placeholder(raw):
                skipped_placeholder += count
                continue
            key = self._norm_strain_name(raw)
            b = buckets.setdefault(key, {'count': 0, 'variants': {}})
            b['count'] += count
            b['variants'][raw] = b['variants'].get(raw, 0) + count

        # Existing strain_type per raw value, fetched ONCE. Looking this up per
        # bucket instead would fire ~2.9k GROUP BY queries against an unindexed
        # 84k-row char column during the migration.
        type_by_raw = self._strain_type_distribution()

        index = self._build_name_index()
        created = 0
        aliased = 0
        skipped_thin = 0

        for key, bucket in buckets.items():
            if bucket['count'] < min_count:
                skipped_thin += 1
                continue

            # Canonical display name = the most-used raw spelling, minus its
            # type tag. Ties break on the alphabetically first spelling so the
            # seed is deterministic across runs and environments.
            variants = sorted(bucket['variants'].items(), key=lambda kv: (-kv[1], kv[0]))
            top_raw = variants[0][0]
            display = _TYPE_SUFFIX_RE.sub('', top_raw.strip()).rstrip('*').strip() or top_raw.strip()

            # strain_type: first from a type tag on any variant, else the most
            # common strain_type already set on that bucket's products.
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

            # Record every raw spelling that differs from the master name as an
            # alias, so resolve_name() folds future importer writes onto it.
            have = {self._norm_strain_name(l) for l in (strain.aliases or '').splitlines()}
            have.add(self._norm_strain_name(strain.name))
            new_lines = []
            for raw, _n in variants:
                if raw.strip() and raw.strip() != strain.name and self._norm_strain_name(raw) not in have:
                    new_lines.append(raw.strip())
                    have.add(self._norm_strain_name(raw))
            if new_lines:
                strain.aliases = '\n'.join(
                    [l for l in (strain.aliases or '').splitlines() if l.strip()] + new_lines
                )
                aliased += len(new_lines)

        linked = self._link_products() if link else 0

        summary = {
            'raw_values': len(groups),
            'buckets': len(buckets),
            'created': created,
            'aliases_added': aliased,
            'skipped_thin': skipped_thin,
            'placeholder_rows_skipped': skipped_placeholder,
            'products_linked': linked,
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
    def _link_products(self):
        """Back-fill product.template.strain_id from the raw text, in bulk SQL.

        Raw SQL rather than 70k ORM writes — the ORM path would also fire the
        strain/strain_id sync in product.template.write() and rewrite the very
        text column being read from.

        One temp map + one UPDATE, not one UPDATE per strain: `strain` is an
        unindexed char column on 84k rows, so per-strain statements would mean
        ~4.2k sequential scans.
        """
        # raw text -> strain id. First writer wins, matching _build_name_index,
        # so a name always beats an alias and the result is deterministic even
        # if a curator pastes the same alias onto two strains.
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
        pairs = list(owner.items())
        if not pairs:
            return 0

        cr = self.env.cr
        # Land any pending ORM writes (the alias edits the seed just made)
        # before reading/writing those same rows through raw SQL.
        self.env.flush_all()

        # DROP first: ON COMMIT DROP only fires at commit, so a second call
        # inside one transaction — seed_from_products run twice — would hit
        # "relation already exists".
        cr.execute("DROP TABLE IF EXISTS mint_strain_map")
        cr.execute("""
            CREATE TEMP TABLE mint_strain_map (raw_name VARCHAR, strain_id INTEGER)
            ON COMMIT DROP
        """)
        execute_values(
            cr._obj,
            "INSERT INTO mint_strain_map (raw_name, strain_id) VALUES %s",
            pairs,
            page_size=1000,
        )
        cr.execute("CREATE INDEX ON mint_strain_map (raw_name)")
        cr.execute("""
            UPDATE product_template p
               SET strain_id = m.strain_id
              FROM mint_strain_map m
             WHERE p.strain = m.raw_name
               AND p.strain_id IS DISTINCT FROM m.strain_id
        """)
        linked = cr.rowcount

        # Refresh the stored counter in one statement rather than per-record.
        cr.execute("""
            UPDATE mint_strain s
               SET product_count = (
                     SELECT COUNT(*)
                       FROM product_template p
                      WHERE p.strain_id = s.id
                   )
        """)

        # Both tables were changed behind the ORM's back; drop the cached
        # values so subsequent reads in this transaction see the new rows.
        self.env.invalidate_all()
        return linked
