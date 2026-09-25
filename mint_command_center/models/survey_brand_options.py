"""Survey choice questions whose options mirror the brand catalog.

The Employee Sample Product Review survey asks "Brand Name" as a simple_choice.
Its options used to be typed in by hand, so a new sample brand (Shorties) never
showed up until someone edited the survey. A question flagged
``mint_brand_catalog_options`` is instead reconciled hourly against the brands
that have an employee-sample SKU in the catalog.

The sync only touches the question's answer options — never the questions —
because view 10775 (survey.sample_review_prefill) autofills other questions of
survey 194 by their ids.
"""
import logging
import re
import unicodedata

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# Same token rule as the storefront's isSampleProduct(): "Sampler" packs are
# retail products, not employee samples, so they must not match.
SAMPLE_RE = re.compile(r'\bsamples?\b', re.IGNORECASE)
# Duplicate brands are renamed "Keef [DUP → 403]" / "[MERGED→ 12] Keef" by the
# brand cleanup; the number is the survivor's id.
TOMBSTONE_RE = re.compile(r'\[\s*(?:DUP|MERGED)\s*(?:→|->)\s*(\d*)[^\]]*\]', re.IGNORECASE)
DEAD_RE = re.compile(r'do not use', re.IGNORECASE)


def brand_key(name):
    """Fold case, accents and punctuation: 'Grön' == 'gron', 'Stiiizy_' == 'STIIIZY'."""
    name = TOMBSTONE_RE.sub('', name or '')
    name = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode()
    return re.sub(r'[^a-z0-9]', '', name.lower())


def _dutchie_tokens(brand):
    """Per-LSP Dutchie brand ids ('575:63880'); two rows sharing one are one brand."""
    raw = '%s\n%s' % (brand.dutchie_brand_ids or '', brand.dutchie_brand_id or '')
    return {tok for tok in re.split(r'[\s,]+', raw) if ':' in tok}


# A group bigger than this means a bad alias or Dutchie id is chaining unrelated
# brands together; its members are then kept apart rather than collapsed.
MAX_GROUP = 6


class MintBrand(models.Model):
    _inherit = 'mint.brand'

    @api.model
    def _mint_sample_survey_brand_keys(self):
        """Map every name/alias key of a sampled brand to its canonical brand.

        Brands are grouped when they share a folded name, an alias, or a
        per-LSP Dutchie brand id, so 'Wana Brands', 'BOUTIQ' or 'drip' collapse
        into Wana, Boutiq by Shango and the Dutchie-linked Drip. Canonical =
        Dutchie-linked first, then most products, then lowest id. Only groups
        containing a brand with an employee-sample SKU are returned.
        """
        Brand = self.sudo()
        live = Brand.browse([
            brand.id for brand in Brand.search([])
            if not TOMBSTONE_RE.search(brand.name or '') and not DEAD_RE.search(brand.name or '')
            and not SAMPLE_RE.search(brand.name or '')
        ])

        parent = {}

        def find(node):
            parent.setdefault(node, node)
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        brand_keys = {}
        for brand in live:
            keys = {brand_key(brand.name)}
            keys |= {brand_key(alias) for alias in re.split(r'[\n,]+', brand.aliases or '')}
            keys.discard('')
            brand_keys[brand.id] = keys
            nodes = ['k:' + key for key in keys] + ['d:' + tok for tok in _dutchie_tokens(brand)]
            for node in nodes:
                parent[find(node)] = find(('b', brand.id))

        groups = {}
        for brand in live:
            groups.setdefault(find(('b', brand.id)), []).append(brand)
        # Oversized groups are split back into single brands.
        group_of = {}
        for root, members in groups.items():
            if len(members) > MAX_GROUP:
                _logger.warning('brand group of %d not collapsed: %s', len(members), [b.name for b in members])
                for brand in members:
                    group_of[brand.id] = [brand]
            else:
                for brand in members:
                    group_of[brand.id] = members

        rows = self.env['product.template'].sudo().search_read(
            [('name', 'ilike', 'sample'), ('brand_id', '!=', False)],
            ['name', 'brand_id'],
        )
        sampled = set()
        for row in rows:
            if not SAMPLE_RE.search(row['name'] or ''):
                continue
            brand = Brand.browse(row['brand_id'][0])
            tomb = TOMBSTONE_RE.search(brand.name or '')
            if tomb:
                if not tomb.group(1):
                    continue
                brand = Brand.browse(int(tomb.group(1)))
            if brand.id in group_of:
                sampled.add(brand.id)

        def rank(brand):
            return (not _dutchie_tokens(brand), -(brand.product_count or 0), brand.id)

        result = {}
        for brand_id in sampled:
            members = group_of[brand_id]
            canonical = min(members, key=rank)
            for member in members:
                for key in brand_keys[member.id]:
                    result.setdefault(key, canonical)
        return result


class SurveyQuestionAnswer(models.Model):
    _inherit = 'survey.question.answer'

    mint_brand_id = fields.Many2one(
        'mint.brand', string='Brand', index=True, ondelete='set null',
        help='Catalog brand this option mirrors. Set by the brand-catalog sync.',
    )


class SurveyQuestion(models.Model):
    _inherit = 'survey.question'

    mint_brand_catalog_options = fields.Boolean(
        string='Options from Brand Catalog',
        help='Keep this question\'s options in sync with the brands that have '
             'an employee-sample product in the catalog (Mint Marketing > '
             'Setup > Brands). Runs hourly: adds new brands, sorts A-Z and '
             'removes brands that drop out — except options that already '
             'have responses, which are kept so no answer is lost.',
    )

    @api.model
    def _cron_sync_mint_brand_options(self):
        questions = self.sudo().search([('mint_brand_catalog_options', '=', True)])
        if not questions:
            return
        by_key = self.env['mint.brand']._mint_sample_survey_brand_keys()
        for question in questions:
            question._mint_sync_brand_options(by_key)

    def _mint_brand_answer_map(self):
        """``{str(mint.brand id): answer id}`` for this question's options.

        Options only carry the *canonical* brand of each folded group, but an
        order line's product can point at any member ('Wana Brands', a
        '[DUP → n]' tombstone, a Dutchie-id twin). Every brand whose name key
        folds into a sampled group maps to that group's option, using the same
        fold as the sync, so the survey prefill can pick the option by id.
        """
        self.ensure_one()
        if not self.mint_brand_catalog_options:
            return {}
        by_brand = {
            answer.mint_brand_id.id: answer.id
            for answer in self.sudo().suggested_answer_ids if answer.mint_brand_id
        }
        result = {str(brand_id): answer_id for brand_id, answer_id in by_brand.items()}
        by_key = self.env['mint.brand']._mint_sample_survey_brand_keys()
        for brand in self.env['mint.brand'].sudo().search([]):
            canonical = by_key.get(brand_key(brand.name))
            if canonical and canonical.id in by_brand:
                result.setdefault(str(brand.id), by_brand[canonical.id])
        return result

    def _mint_sync_brand_options(self, by_key):
        self.ensure_one()
        Answer = self.env['survey.question.answer'].sudo()
        brands = self.env['mint.brand'].sudo().browse(sorted({brand.id for brand in by_key.values()}))
        wanted = set(brands.ids)

        linked = {}
        stale = Answer
        # Existing options are adopted by link first, then by name, so the
        # hand-typed legacy labels ("Drip", "Grön") keep their responses.
        for answer in self.sudo().suggested_answer_ids.sorted(lambda a: (a.sequence, a.id)):
            brand = answer.mint_brand_id if answer.mint_brand_id.id in wanted else by_key.get(brand_key(answer.value))
            if brand and brand.id not in linked:
                if answer.mint_brand_id != brand:
                    answer.mint_brand_id = brand
                linked[brand.id] = answer
            else:
                stale |= answer

        created = Answer
        for brand in brands.sorted('id'):
            if brand.id not in linked:
                created |= Answer.create({
                    'question_id': self.id,
                    'value': brand.name,
                    'mint_brand_id': brand.id,
                })
                linked[brand.id] = created[-1:]

        # Deleting an answered option nulls its responses (suggested_answer_id
        # is ondelete='set null'), so those stay until nobody depends on them.
        answered = self.env['survey.user_input.line'].sudo().search(
            [('suggested_answer_id', 'in', stale.ids)]
        ).suggested_answer_id
        removed = stale - answered
        removed_names = removed.mapped('value')
        removed.unlink()

        options = list(linked.values()) + list(stale & answered)
        options.sort(key=lambda a: (brand_key(a.value), a.id))
        for seq, answer in enumerate(options, start=1):
            if answer.sequence != seq * 10:
                answer.sequence = seq * 10

        _logger.info(
            'survey.question %s brand options: %d total, %d added %s, %d removed %s, %d kept for responses',
            self.id, len(options), len(created), created.mapped('value'),
            len(removed_names), removed_names, len(stale & answered),
        )
