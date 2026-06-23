"""Dutchie discount publishing for vendor deal submissions.

Trigger: the existing final-approval transition — ``action_convert_to_deal``
on ``mint.deal.submission`` (state -> scheduled, which already sits behind
submission approval AND the national-promo plot gate). After the PTL deal is
built, the submission's structured targeting is transformed into a Dutchie
Backoffice discount payload and handed to the inventory service:

    POST {dutchie.publish.url}/api/admin/discounts   {locId, lspId, discount}

Modes (ir.config_parameter ``dutchie.publish.mode``):
    off      — do nothing.
    dry_run  — build the payload(s) and post them to the submission chatter;
               NO network write. This is the default.
    live     — POST ONE multi-store record per contiguous-schedule span; every
               target store rides in the discount's LocationRestrictions array
               (requires ``dutchie.publish.api_key``). This replaced an earlier
               per-LocId loop that created one duplicate discount per store.

Safety: ExternalId is always ``lgm_<submission_id>`` (round-trip identity +
the lgm-prefix rule for records we create in Dutchie). Publish failures never
roll back the conversion — they are reported on chatter only.

Value conventions follow docs/mappings/discount-canonical.json:
PERCENT_OFF (calc 2) DiscountValue is a 0-1 fraction (50% -> 0.5);
bundle "N for $X" is calc 6 with DiscountValue=X and ThresholdMin=N.
"""
import json
import logging
import re
import urllib.request
from datetime import date as _date, timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError

from .deal_mixins import (format_bundle_tiers_text, build_dutchie_restrictions,
                          normalize_publish_mode, dutchie_deal_external_id)

_logger = logging.getLogger(__name__)

# mint.region name -> Dutchie LSP (tenant). BrandIds/ProductIds are LSP-scoped.
LSP_BY_REGION = {
    'arizona': 575,
    'michigan': 576,
    'missouri': 723,
    'illinois': 805,
    'nevada': 820,
    'florida': 821,
}

# submission discount_type -> Dutchie CalculationMethodId (canonical registry)
CALC_BY_TYPE = {
    'percent': 2,
    'fixed': 1,
    'price': 3,
    'bundle': 6,
    'points_multiplier': 15,
    # Clearance (near-expiry) publishes as a straight PERCENT_OFF — it carries
    # a percent value like 'percent' (_format_sales_details renders
    # "N% Off (Clearance)"). Without this, _dutchie_build raised "Unsupported
    # discount type 'clearance'" — swallowed by the convert try/except — so
    # clearance deals silently never reached Dutchie.
    'clearance': 2,
}

DAY_KEYS = ('Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday')

# Vendor free-text that means "no exclusion", not a product term.
NOISE = {'', 'none', 'none.', 'na', 'n/a', 'no', 'nope', 'yes', 'all', 'all skus', '-'}
NOISE_PREFIXES = ('all ', 'everything', 'no sku', "no sku's", 'n/a')

RE_PCT = re.compile(r'(\d+(?:\.\d+)?)\s*%\s*off', re.I)
RE_NFOR = re.compile(r'(\d+)\s*for\s*\$\s*(\d+(?:\.\d+)?)', re.I)
RE_DOLLAR = re.compile(r'\$\s*(\d+(?:\.\d+)?)\s*off', re.I)
RE_BOGO = re.compile(r'\bbogo\b|\bb1g1\b|buy\s+one,?\s+get\s+one', re.I)


class DealSubmissionDutchiePublish(models.Model):
    _inherit = 'mint.deal.submission'

    dutchie_publish_loc_ids = fields.Text(
        string='Dutchie Published ExternalId→DiscountId (JSON)',
        copy=False,
        help='Identity of the published Dutchie discounts from the last '
             'successful live publish — JSON {"<externalId>": <dutchieId>}, one '
             'id per contiguous-schedule span (ExternalId lgm_<id> or '
             'lgm_<id>_w<k>). Each span is ONE multi-store record (all stores '
             'ride in LocationRestrictions), so re-publish UPDATES the recorded '
             'id in place instead of creating duplicates (payload otherwise '
             'hardcodes Id=0 = always-create). Legacy per-loc shapes '
             '({"<locId>": id} or {"<externalId>": {"<locId>": id}}) are still '
             'understood for deactivation but re-create as consolidated on '
             'next publish.',
    )

    # ------------------------------------------------------------------
    # #2 — discount value parsing (regex + reviewer gate)
    # ------------------------------------------------------------------

    @api.model
    def _parse_discount_from_text(self, text):
        """Parse ONE unambiguous offer out of vendor prose, or None.

        Fills only when the text contains exactly one kind of offer with one
        value ("50% off ...", "2 for $18", "$10 off", "BOGO"). Multi-deal
        blobs (different percentages per day, mixed bundles) return None and
        stay a reviewer task — guessing wrong on price is worse than manual.
        """
        text = text or ''
        pcts = {float(v) for v in RE_PCT.findall(text)}
        nfors = {(int(n), float(v)) for n, v in RE_NFOR.findall(text)}
        dollars = {float(v) for v in RE_DOLLAR.findall(text)}
        bogo = bool(RE_BOGO.search(text))
        kinds = sum(1 for x in (pcts, nfors, dollars, bogo) if x)
        if kinds != 1:
            return None
        if pcts:
            if len(pcts) != 1:
                return None
            return {'discount_type': 'percent', 'discount_value': pcts.pop()}
        if nfors:
            if len(nfors) != 1:
                return None
            n, amount = nfors.pop()
            return {'discount_type': 'bundle', 'discount_value': amount,
                    'threshold_min': n}
        if dollars:
            if len(dollars) != 1:
                return None
            return {'discount_type': 'fixed', 'discount_value': dollars.pop()}
        return {'discount_type': 'bogo', 'discount_value': 0.0}

    @api.model_create_multi
    def create(self, vals_list):
        # Auto-fill discount_value from the prose when the submission arrives
        # without one (JotForm imports, sparse web-form entries). Only fills a
        # blank — never overwrites reviewer input.
        for vals in vals_list:
            if not vals.get('discount_value') and vals.get('sales_details'):
                parsed = self._parse_discount_from_text(vals['sales_details'])
                # Fill only when the parsed offer kind matches the incoming
                # type (or none was given) — no cross-type value pollution.
                if parsed and vals.get('discount_type') in (None, False, '', parsed['discount_type']):
                    vals['discount_type'] = parsed['discount_type']
                    vals['discount_value'] = parsed['discount_value']
        return super().create(vals_list)

    def action_parse_discount(self):
        """Manual re-parse (list/form server action) — fills blanks only."""
        filled = 0
        for sub in self:
            if sub.discount_value:
                continue
            parsed = self._parse_discount_from_text(sub.sales_details)
            if parsed and (not sub.discount_type
                           or parsed['discount_type'] == sub.discount_type):
                sub.write({
                    'discount_type': sub.discount_type or parsed['discount_type'],
                    'discount_value': parsed['discount_value'],
                })
                filled += 1
        return filled

    # ------------------------------------------------------------------
    # #1 — publish on the existing final-approval transition
    # ------------------------------------------------------------------

    def action_convert_to_deal(self):
        self.ensure_one()
        mode = normalize_publish_mode(self.env['ir.config_parameter'].sudo()
                                      .get_param('dutchie.publish.mode'))
        # #2 reviewer gate: a deal can't convert (and thus can't publish) with
        # no numeric value. Exemptions: bogo (valid without one), structured
        # bundles (#93677 — pricing lives in bundle_tier_ids, and the
        # auto-generated multi-tier text defeats the RE_NFOR parse), and
        # mode=off (publishing disabled — don't block legacy conversions).
        # NOTE: clearance is NOT exempt — it maps to PERCENT_OFF and needs its
        # percent, so a value-less clearance is caught here with a clear
        # message instead of publishing a 0%-off discount.
        if (mode != 'off' and not self.discount_value
                and self.discount_type != 'bogo'
                and not (self.discount_type == 'bundle' and self.bundle_tier_ids)):
            parsed = self._parse_discount_from_text(self.sales_details)
            # Only trust the parse when its offer kind matches the chosen
            # type (or no type was chosen) — never turn "$10 off" prose into
            # a 10% discount because discount_type said percent.
            if parsed and (not self.discount_type
                           or parsed['discount_type'] == self.discount_type):
                self.write({'discount_value': parsed['discount_value'],
                            'discount_type': self.discount_type or parsed['discount_type']})
            else:
                raise UserError(
                    "Discount Value is not set and the Sales Details text is "
                    "ambiguous (multiple offers, or its offer kind doesn't "
                    "match the chosen Discount Type). Enter the value/type "
                    "for the deal being approved, then convert again."
                )
        res = super().action_convert_to_deal()
        try:
            self._dutchie_publish_after_convert()
        except Exception as exc:  # never break conversion on publish issues
            _logger.exception("Dutchie publish failed for submission %s", self.id)
            try:
                self.message_post(
                    body=f"Dutchie publish FAILED (conversion unaffected): {exc}",
                    message_type='comment',
                )
            except Exception:
                # Chatter itself can crash (rogue automation on message_post);
                # the log line above is the fallback record.
                _logger.exception("…and the failure chatter post also failed (submission %s)", self.id)
        return res

    def action_publish_to_dutchie(self):
        """Manual (re)publish for an already-converted submission.

        Auto-publish on convert never blocks the conversion, so a transient or
        data error leaves the PTL deal created but nothing in Dutchie. This
        button re-fires the publish. Unlike the auto path, errors surface
        DIRECTLY to the reviewer (normal popup) so they can fix and retry.
        """
        self.ensure_one()
        if not self.deal_id:
            raise UserError(
                "This submission hasn't been converted to a PTL deal yet — "
                "Schedule it first (publishing fires automatically on convert)."
            )
        mode = (self.env['ir.config_parameter'].sudo()
                .get_param('dutchie.publish.mode') or 'dry_run').strip().lower()
        if mode == 'off':
            raise UserError("Dutchie publishing is turned off (dutchie.publish.mode=off).")
        # Surfaces hard failures (no LSP / no restrictions / BOGO guard) as a
        # popup; posts the payload (dry_run) or per-store HTTP result (live) to
        # chatter either way.
        self._dutchie_publish_after_convert()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'title': "Dutchie publish",
                'message': ("Dry-run payload posted to the chatter."
                            if mode == 'dry_run'
                            else "Publish attempted — see the chatter for the per-store result."),
                'sticky': False,
            },
        }

    def action_open_dutchie_publish_review(self):
        """Open the pre-publish review wizard (guards + alerts) for the
        submission Publish-to-Dutchie button. Warn-only: shows mode, target
        LocIds and SOP/safety alerts; action_publish_to_dutchie fires on
        confirm."""
        self.ensure_one()
        from .dutchie_publish_review import build_review_html
        mode, is_live, lines, warnings, blocks = self._dutchie_publish_review_data()
        wiz = self.env['mint.dutchie.publish.review'].create({
            'res_model': self._name,
            'res_id': self.id,
            'mode': mode,
            'is_live': is_live,
            'review_html': build_review_html(mode, is_live, lines, warnings, blocks),
        })
        return {
            'type': 'ir.actions.act_window',
            'name': 'Review before publishing',
            'res_model': 'mint.dutchie.publish.review',
            'res_id': wiz.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def _dutchie_publish_review_data(self):
        """Compute (mode, is_live, lines, warnings, blocks) for the submission
        Dutchie publish WITHOUT writing anything (used by the review wizard)."""
        self.ensure_one()
        get_param = self.env['ir.config_parameter'].sudo().get_param
        mode = (get_param('dutchie.publish.mode') or 'dry_run').strip().lower()
        is_live = mode == 'live'
        lines, warnings, blocks = [], [], []
        if mode == 'off':
            warnings.append("dutchie.publish.mode is 'off' — publishing is disabled; confirming does nothing.")
        elif mode not in ('dry_run', 'live'):
            warnings.append("Unrecognized publish mode %r — this path treats anything that isn't 'off'/'dry_run' as LIVE (fail-open). Fix dutchie.publish.mode." % mode)
            is_live = True
        try:
            built = self._dutchie_build()
            discounts, lsp = built['discounts'], built['lsp']
            warnings.extend(built['warnings'])
            loc_map = json.loads(get_param('dutchie.publish.loc_ids') or '{}')
            loc_ids = loc_map.get(str(lsp)) or []
            lines.append("Vendor: %s — %s" % (
                self.vendor_name, discounts[0].get('OnlineName') if discounts else '?'))
            if len(discounts) > 1:
                lines.append("Schedule has gap(s) → %d SEPARATE Dutchie discounts (one per contiguous span):"
                             % len(discounts))
            for gi, discount in enumerate(discounts, 1):
                prefix = ("  • span %d: " % gi) if len(discounts) > 1 else "Dates: "
                active = [d for d in DAY_KEYS if discount.get(d)]
                lines.append("%s%s → %s  [%s]" % (
                    prefix, discount.get('ValidDateFrom'), discount.get('ValidDateTo'),
                    ', '.join(active) if active else 'EVERY DAY (no day restriction)'))
                if not active:
                    warnings.append("Span %d has no day-of-week restriction — Dutchie applies it EVERY day in the range." % gi)
            lines.append("LSP %s → %d LocId(s) (one multi-store record per span): %s"
                         % (lsp, len(loc_ids), ', '.join(map(str, loc_ids)) or 'NONE'))
            lines.append("Total Dutchie writes: %d discount(s) (one per span), each ONE record across %d store(s)"
                         % (len(discounts), len(loc_ids)))
            if not loc_ids:
                warnings.append("No LocIds configured for LSP %s (dutchie.publish.loc_ids) — nothing will publish." % lsp)
            if self.store_ids:
                warnings.append("This submission is scoped to %d store(s), but Publish-to-Dutchie publishes to ALL %d LocId(s) for the market (one multi-store discount per span) — per-store scoping is IGNORED on this path."
                                % (len(self.store_ids), len(loc_ids)))
            try:
                prior = json.loads(self.dutchie_publish_loc_ids or '{}')
            except (ValueError, TypeError):
                prior = {}
            if prior:
                consolidated = sum(1 for v in prior.values()
                                   if isinstance(v, int) and not isinstance(v, bool))
                legacy = len(prior) - consolidated
                note = "Re-publish: %d span(s) recorded → UPDATE in place (no duplicates)." % consolidated
                if legacy:
                    note += " %d legacy per-loc record(s) will re-create as consolidated (clean up the old duplicates)." % legacy
                lines.append(note)
        except Exception as exc:
            blocks.append("Payload build failed — Publish would raise: %s" % exc)
        return mode, is_live, lines, warnings, blocks

    # ------------------------------------------------------------------
    # Payload assembly
    # ------------------------------------------------------------------

    def _dutchie_lsp(self):
        self.ensure_one()
        name = (self.market_id.name or '').strip().lower() if self.market_id else ''
        for key, lsp in LSP_BY_REGION.items():
            if key in name:
                return lsp
        return None

    def _dutchie_brands(self):
        """Brand records this deal targets: brand_id, else alias resolution
        of the vendor string (#3 — may return several for multi-brand)."""
        self.ensure_one()
        brands = (self.brand_ids | self.brand_id) if self.brand_id else self.brand_ids
        if brands:
            return brands
        return self.env['mint.brand'].resolve_vendor_string(self.vendor_name)

    def _exclusion_terms(self):
        """Clean vendor exclusion prose into searchable product terms (#4)."""
        terms = []
        for raw in re.split(r'[\n,;]+', self.excluded_skus or ''):
            t = raw.strip()
            low = t.lower()
            if not t or low in NOISE or any(low.startswith(p) for p in NOISE_PREFIXES):
                continue
            # strip directive lead-ins: "No ...", "Exclude/Excluding ...",
            # "This deal excludes ..."
            t = re.sub(r'^(this\s+deal\s+excludes?|excludes?|excluding|no)\s+',
                       '', t, flags=re.I).strip(' .!')
            if t:
                terms.append(t)
        return terms

    def _resolve_exclusion_products(self, brands):
        """Brand-scoped term -> product resolution. Returns
        (dutchie_product_ids, unresolved_terms)."""
        Product = self.env['product.template']
        ids, unresolved = [], []
        for term in self._exclusion_terms():
            domain = [('name', 'ilike', term), ('dutchie_product_id', '!=', False)]
            if brands:
                domain = [('brand_id', 'in', brands.ids)] + domain
            hits = Product.search(domain, limit=500)
            got = [int(p.dutchie_product_id) for p in hits
                   if str(p.dutchie_product_id or '').strip().isdigit()]
            if got:
                ids.extend(got)
            else:
                unresolved.append(term)
        return list(dict.fromkeys(ids)), unresolved

    @staticmethod
    def _dutchie_date(d, end_of_day=False):
        return f"{d.month}/{d.day}/{d.year}, " + ("11:59:59 PM" if end_of_day else "12:00:00 AM")

    @staticmethod
    def _partition_faithful(dates):
        """Partition sorted, unique date objects into 'faithful' groups.

        A group is faithful when collapsing it to [min, max] + the union of its
        weekdays reproduces EXACTLY the group's dates — no date inside the span
        that matches one of the group's weekdays is missing from it. Greedy
        left-to-right: extend the current group while it stays faithful, else
        start a new one. An unbroken weekly recurrence (every Thu/Fri/Sat for
        12 weeks) stays ONE group; a real calendar gap forces a split so the
        published Dutchie discount never fires on un-plotted days between
        windows. Returns a list of date-object lists (each already sorted)."""
        groups = []
        cur = []
        for d in dates:
            if not cur:
                cur = [d]
                continue
            allowed = set(cur) | {d}
            dows = {x.weekday() for x in allowed}
            probe, ok = cur[0], True
            while probe <= d:
                if probe.weekday() in dows and probe not in allowed:
                    ok = False
                    break
                probe += timedelta(days=1)
            if ok:
                cur.append(d)
            else:
                groups.append(cur)
                cur = [d]
        if cur:
            groups.append(cur)
        return groups

    def _dutchie_build(self):
        """Build {lsp, discount, warnings} for this submission, or raise."""
        self.ensure_one()
        warnings = []
        lsp = self._dutchie_lsp()
        if not lsp:
            raise UserError(f"No Dutchie LSP mapping for market {self.market_id.name!r}.")

        brands = self._dutchie_brands()
        brand_ids = []
        for b in brands:
            bid = b.dutchie_brand_id_for_lsp(lsp)
            if bid and str(bid).isdigit():
                brand_ids.append(int(bid))
            else:
                warnings.append(f"brand {b.name!r} has no Dutchie id for LSP {lsp}")
        if not brands:
            warnings.append(f"vendor {self.vendor_name!r} resolved to no brand (add an alias on mint.brand)")

        prod_inc = [int(p.dutchie_product_id) for p in self.product_ids
                    if str(p.dutchie_product_id or '').strip().isdigit()]
        unres_inc = [p.name for p in self.product_ids if not p.dutchie_product_id]
        if unres_inc:
            warnings.append("products without dutchie id skipped: " + ", ".join(unres_inc[:5]))
        prod_exc, unres_exc = self._resolve_exclusion_products(brands)
        if unres_exc:
            warnings.append("exclusion terms unresolved: " + ", ".join(unres_exc))

        exc_brand_ids = []
        for b in self.excluded_brand_ids:
            bid = b.dutchie_brand_id_for_lsp(lsp)
            if bid and str(bid).isdigit():
                exc_brand_ids.append(int(bid))

        # Category restriction. Structured picks first: the In-Stock
        # Categories dropdown stores the exact product.category records the
        # submitter chose — emit their Dutchie ids directly, no name
        # matching. Legacy free-text fallback: the field may carry a MASTER
        # bucket ('Edibles & Tinctures', ...), one Dutchie category name, or
        # a comma-joined list of names (the dropdown mirror writes 'A, B, C'
        # — treating that as ONE name matched nothing and silently published
        # the deal with no category restriction at all).
        cat_ids = []
        picked = self._all_picked_categories()
        if picked:
            cat_ids = sorted({int(c.dutchie_category_id) for c in picked
                              if str(c.dutchie_category_id or '').strip().isdigit()})
            unres = [c.name for c in picked
                     if not str(c.dutchie_category_id or '').strip().isdigit()]
            if unres:
                warnings.append(
                    "categories without Dutchie id skipped: " + ", ".join(unres))
        elif self.product_category and self.product_category.strip().lower() not in NOISE:
            from .ptl_deal import MASTER_CATEGORY_PATTERNS
            Categ = self.env['product.category']
            cats = Categ.browse([])
            for token in (t.strip() for t in self.product_category.split(',')):
                if not token or token.lower() in NOISE:
                    continue
                patterns = MASTER_CATEGORY_PATTERNS.get(token)
                if patterns:
                    domain = ['|'] * (len(patterns) - 1) + [('name', 'ilike', p) for p in patterns]
                    cats |= Categ.search(domain)
                else:
                    cats |= Categ.search([('name', 'ilike', token)])
            cat_ids = sorted({int(c.dutchie_category_id) for c in cats
                              if str(c.dutchie_category_id or '').strip().isdigit()})
        if (picked or (self.product_category
                       and self.product_category.strip().lower() not in NOISE)) \
                and not cat_ids:
            warnings.append(
                f"category {self.product_category!r} resolved to no Dutchie category ids")

        # Active window + day-of-week from the structured plot windows.
        # all_dates() returns ISO strings — coerce to date objects.
        dates = [_date.fromisoformat(d) if isinstance(d, str) else d
                 for d in (self.window_ids.all_dates() if self.window_ids else [])]
        if not dates and self.preferred_start_date:
            cur = self.preferred_start_date
            end = self.preferred_end_date or cur
            while cur <= end:
                dates.append(cur)
                cur += timedelta(days=1)
        if not dates:
            raise UserError("No schedule: add Plot Windows (or preferred dates) before converting.")
        dates = sorted(set(dates))
        # Split into FAITHFUL groups so a non-contiguous schedule (e.g. early
        # July + late September, skipping August) becomes SEPARATE Dutchie
        # discounts — collapsing every plotted date to a single [min, max]
        # range + weekday union would also activate the discount on matching
        # weekdays inside the gap, days that were never scheduled. An unbroken
        # weekly recurrence stays a SINGLE group. Day-of-week flags are derived
        # per group below.
        groups = self._partition_faithful(dates)

        # BOGO: Dutchie has no native BOGO type. The live-verified encoding
        # (discounts 379870 "BOGO 50%" / 379191 "BOGO" at Tempe, read raw
        # 2026-06-12) is PERCENT_OFF + NUMBER_OF_ITEMS threshold 2 +
        # ApplyToOnlyOneItem=True: buy two, the discount hits ONE of them.
        # discount_value carries the percent (50 = "BOGO 50% off");
        # empty/0 means the second item is free (100%).
        is_bogo = self.discount_type == 'bogo'
        calc = 2 if is_bogo else CALC_BY_TYPE.get(self.discount_type)
        if not calc:
            raise UserError(f"Unsupported discount type {self.discount_type!r} for Dutchie publish.")
        value = float(self.discount_value or 0)
        threshold_min = None
        apply_to_one = False
        if is_bogo:
            # Guard: type=bogo with a blank value but percent prose ("BOGO
            # 50% off ...") would silently publish a FREE item. Make the
            # reviewer set the value explicitly.
            if not value and RE_PCT.search(self.sales_details or ''):
                raise UserError(
                    "This BOGO's Sales Details mention a percentage but "
                    "Discount Value is empty — set it (e.g. 50 for 'BOGO "
                    "50% off') or it would publish as a FREE item."
                )
            value = (value / 100.0) if value else 1.0
            threshold_min = 2
            apply_to_one = True
        elif calc == 2:
            # Model convention is WHOLE percents (50 = 50% — see
            # _format_sales_details). Divide unconditionally: a stored 1
            # means 1% (0.01), never 100%.
            value = value / 100.0
        if calc == 2 and value > 1.0:
            raise UserError(
                f"Discount value {value * 100:g}% exceeds 100% — refusing to publish."
            )
        elif calc == 6:
            if self.bundle_tier_ids:
                # Structured tiers (#93677) are the source of truth: first
                # tier's qty/price. Dutchie's calc-6 reward holds a single
                # (threshold, total) pair, so extra tiers need their own
                # Backoffice discount — surface that instead of dropping
                # them silently.
                first = self.bundle_tier_ids[0]
                threshold_min = first.qty
                value = float(first.price)
                if len(self.bundle_tier_ids) > 1:
                    extra = format_bundle_tiers_text(
                        [(t.qty, t.price) for t in self.bundle_tier_ids[1:]])
                    warnings.append(
                        f"only the first bundle tier was published; build the "
                        f"remaining tier(s) ({extra}) manually in Backoffice")
            else:
                m = RE_NFOR.search(self.sales_details or '')
                threshold_min = int(m.group(1)) if m else 2

        # Brand/Product/Category restriction assembly lives in a pure helper so
        # it is unit-testable in isolation (see tests/test_dutchie_restrictions).
        # The Product include is deliberately dropped when a Brand/Category
        # already scopes the deal — Dutchie AND's restriction types, so layering
        # would only shrink eligibility (sub 395: 233 → 48).
        restrictions, _restr_warns = build_dutchie_restrictions(
            brand_ids, exc_brand_ids, prod_inc, prod_exc, cat_ids)
        warnings += _restr_warns

        # Refuse a discount with NO restrictions at all — that would apply
        # store-wide (every product, every brand). Reachable when none of the
        # deal's brands resolve to a per-LSP Dutchie id and there are no
        # product/category restrictions either.
        if not any(r['RestrictionIds'] for r in restrictions.values()):
            raise UserError(
                "Refusing to publish: no Brand/Product/Category restriction "
                "resolved — the discount would apply store-wide. "
                + ("; ".join(warnings) or "")
            )

        label = (("BOGO" if value >= 1.0 else f"BOGO {value * 100:g}% Off") if is_bogo
                 else f"{value * 100:g}% Off" if calc == 2
                 else f"{threshold_min} for ${value:g}" if calc == 6
                 else f"${value:g} Off" if calc == 1
                 else f"${value:g}")
        # One Dutchie discount per faithful date-group. All non-date fields are
        # shared; only ValidDate*, the day-of-week flags, and ExternalId vary.
        # A single group keeps the historic un-suffixed ExternalId (lgm_<id>)
        # so already-published deals UPDATE in place; multiple groups get
        # lgm_<id>_w<k> so each gap-separated span is its own Dutchie record.
        n_groups = len(groups)
        discounts = []
        for gidx, grp in enumerate(groups, 1):
            gdows = {d.weekday() for d in grp}
            # python weekday Mon=0; DAY_KEYS starts Sunday (idx 0 -> py 6).
            day_flags = {key: ((i - 1) % 7) in gdows
                         for i, key in enumerate(DAY_KEYS)}
            # Unified deal-keyed ExternalId (#2 Stage 2) when a deal exists
            # (post-conversion / publish). Pre-conversion previews (review wizard,
            # no deal yet) keep the legacy submission key — cosmetic only.
            if self.deal_id:
                external_id = dutchie_deal_external_id(
                    self.deal_id.id, None if n_groups == 1 else gidx)
            else:
                external_id = (f"lgm_{self.id}" if n_groups == 1
                               else f"lgm_{self.id}_w{gidx}")
            discounts.append({
                'Id': 0,
                'ApplicationMethodId': 1,
                'CanStackAutomatically': False,
                'Constraints': [],
                'DiscountDescription': f"lgm | {self.vendor_name} (Odoo sub {self.id})",
                'ExternalId': external_id,
                'FirstTimeCustomerOnly': 0,
                'IgnoreNetTax': False,
                'IsAvailableOnline': True,
                'IsBundledDiscount': calc == 6,
                # Filled at dispatch with every target store (one multi-store
                # record per span); empty here so the build stays loc-agnostic.
                'LocationRestrictions': [],
                'OnlineName': f"{self.vendor_name} — {label}",
                'PaymentRestrictions': {'PayByBankSignupIncentive': False},
                # Redemption limit OMITTED → unlimited. Every working live
                # Dutchie discount carries these null/unset; a hardcoded 0/''
                # makes Dutchie reject the discount as unpublishable (see
                # scripts/dutchie/publish-deal.mjs). mint.deal.submission has no
                # per-customer-limit field, so there is nothing to cap on.
                'RequireManagerApproval': False,
                'RestrictToGroupIds': [],
                'RestrictToSegmentIds': [],
                'PlatformTypeRestrictions': [{'PlatformTypeId': 2, 'IsExclusion': False}],
                'OrderTypeRestrictions': [],
                'Reward': {
                    'DiscountRewardId': None,
                    'HasThreshold': bool(threshold_min) or calc in (5, 6),
                    'ApplyToOnlyOneItem': apply_to_one,
                    'CalculationMethodId': calc,
                    'DiscountValue': value,
                    'IncludeNonCannabis': False,
                    # 5 = single-item discount, 6 = bundle grouping (see
                    # dutchie_discount_push.py docs). Live BOGO records use 5.
                    'ItemGroupTypeId': 5 if is_bogo else 6,
                    'ManualDefaultApplyTo': 1,
                    'Restrictions': restrictions,
                    'ThresholdMax': None,
                    'ThresholdMin': threshold_min,
                    'ThresholdTypeId': 1 if threshold_min else 2 if calc == 5 else 0,
                },
                'SavedWithAdvancedOptions': False,
                'ValidDateFrom': self._dutchie_date(grp[0]),
                'ValidDateTo': self._dutchie_date(grp[-1], end_of_day=True),
                'DiscountCode': '',
                # MaxRedemptions / RedemptionLimitCountingMode omitted →
                # unlimited (see RedemptionLimit note above).
                **day_flags,
                'MenuDisplayRank': 0,
                'DiscountMenuDisplayDetails': {
                    'DiscountId': 0,
                    'MenuDisplayImageUrl': '',
                    'MenuDisplayName': self.vendor_name or '',
                    'MenuDisplayDescription': '',
                    'DiscountMenuDisplayId': None,
                },
            })
        return {'lsp': lsp, 'discounts': discounts, 'warnings': warnings}

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _deal_audit_log(self, action, lsp, loc_id, discount, **extra):
        """Structured deal-audit line. Ships to Grafana Loki via
        mint_loki_logger (any _logger.info on this instance is forwarded),
        greppable in Railway logs either way. One line per deal x store
        action so LogQL can count/filter on the JSON fields."""
        rew = discount.get('Reward') or {}
        restr = rew.get('Restrictions') or {}
        entry = {
            'event': 'deal.audit',
            'action': action,
            'submission_id': self.id,
            'external_id': discount.get('ExternalId'),
            'name': discount.get('OnlineName') or discount.get('DiscountDescription'),
            'lsp_id': lsp,
            'loc_id': loc_id,
            'calc_method_id': rew.get('CalculationMethodId'),
            'value': rew.get('DiscountValue'),
            'valid_from': discount.get('ValidDateFrom'),
            'valid_to': discount.get('ValidDateTo'),
            'brand_ids': (restr.get('Brand') or {}).get('RestrictionIds') or [],
            'category_ids': (restr.get('Category') or {}).get('RestrictionIds') or [],
            'user_id': self.env.uid,
            'user': self.env.user.login,
        }
        entry.update(extra)
        _logger.info("deal.audit %s", json.dumps(entry, default=str))

    def _dutchie_publish_after_convert(self):
        self.ensure_one()
        get_param = self.env['ir.config_parameter'].sudo().get_param
        mode = normalize_publish_mode(get_param('dutchie.publish.mode'))
        if mode == 'off':
            return
        built = self._dutchie_build()
        lsp, discounts, warnings = built['lsp'], built['discounts'], built['warnings']
        loc_map = json.loads(get_param('dutchie.publish.loc_ids') or '{}')
        loc_ids = list(dict.fromkeys(int(x) for x in (loc_map.get(str(lsp)) or [])))
        # #3: honor the submission's selected stores — publish only to the
        # configured LocIds that map to store_ids. Without this, path-1 published
        # to EVERY store in the LSP regardless of the deal's scope. If the
        # selection maps to none, leave loc_ids empty so the live guard below
        # refuses to publish (never fall back to store-wide).
        if self.store_ids:
            want = {int(getattr(s, 'dutchie_pos_location_id', 0) or 0)
                    for s in self.store_ids
                    if int(getattr(s, 'dutchie_pos_location_id', 0) or 0)}
            scoped = [l for l in loc_ids if l in want]
            if want and not scoped:
                warnings.append(
                    "None of the %d selected store(s) map to a configured publish "
                    "LocId for LSP %s — skipping (no store-wide fallback)."
                    % (len(self.store_ids), lsp))
            elif len(scoped) != len(loc_ids):
                warnings.append(
                    "Scoped to %d of %d configured LocId(s) by the deal's selected "
                    "stores." % (len(scoped), len(loc_ids)))
            loc_ids = scoped
        # One multi-store record per span: every target store rides in
        # LocationRestrictions. Stamp it on each built span so dry-run shows the
        # real payload and the live path posts the consolidated record.
        for d in discounts:
            d['LocationRestrictions'] = loc_ids

        if mode == 'dry_run':
            spans = "\n".join(
                f"  span {i}: {d['ValidDateFrom']} → {d['ValidDateTo']} "
                f"[{', '.join(k for k in DAY_KEYS if d.get(k)) or 'EVERY DAY'}] "
                f"ExternalId={d['ExternalId']}"
                for i, d in enumerate(discounts, 1))
            body = (
                f"[Dutchie publish — DRY RUN]\n"
                f"LSP {lsp} | {len(discounts)} discount(s), each ONE record across "
                f"{len(loc_ids)} LocId(s): {loc_ids or '(none configured)'}\n"
                f"{spans}\n"
                f"Warnings: {'; '.join(warnings) or 'none'}\n"
                f"Payload (span 1):\n{json.dumps(discounts[0], indent=1) if discounts else '(none)'}"
            )
            self.message_post(body=body, message_type='comment')
            for d in discounts:
                self._deal_audit_log('publish_dry_run', lsp, None, d,
                                     target_loc_ids=loc_ids)
            return

        # live
        # Cross-path mutex (#2 stopgap): if this deal's live Dutchie discount is
        # already owned by the PTL Publish path, do NOT also publish from the
        # convert path — that creates a duplicate record (different ExternalId
        # and topology). First live writer wins; same-path re-publish is allowed.
        # Clear mint.ptl.deal.dutchie_publish_owner to switch paths.
        if self.deal_id and not self.deal_id._dutchie_claim('submission'):
            msg = ("Skipped convert-time Dutchie publish: this deal is already "
                   "published via the PTL path (dutchie_publish_owner='ptl'). "
                   "Avoids a duplicate discount — republish from the PTL deal, or "
                   "clear the owner to switch paths.")
            _logger.info("submission %s: %s", self.id, msg)
            try:
                self.message_post(body=msg, message_type='comment')
            except Exception:
                pass
            return
        url = (get_param('dutchie.publish.url')
               or 'https://mintinvsvc-production-6aa5.up.railway.app').rstrip('/')
        api_key = get_param('dutchie.publish.api_key')
        if not api_key:
            raise UserError("dutchie.publish.api_key is not configured for live mode.")
        if not loc_ids:
            raise UserError(f"dutchie.publish.loc_ids has no LocIds for LSP {lsp}.")
        # ONE multi-store Dutchie discount PER span (not one per store). Dutchie
        # carries every target store in the discount's LocationRestrictions (a
        # flat LocId array) on a SINGLE record — verified against live records
        # spanning all AZ stores. The old per-(span, loc) loop created N
        # duplicate discounts per span (one per store, each store-wide because
        # LocationRestrictions was empty). The owning LocId must be non-zero (the
        # invsvc /api/admin/discounts route rejects locId=0), so each span
        # publishes under loc_ids[0] with every loc listed in LocationRestrictions.
        #
        # Recorded map is {externalId: dutchieId} (one id per span). Legacy maps
        # — flat {locId: id} or nested {externalId: {locId: id}} — are NOT reused
        # for update here (their values aren't a span→single-id int), so a deal
        # published the old way re-creates as consolidated records; its stale
        # per-store duplicates are cleaned up separately (and _dutchie_deactivate
        # still understands the legacy shapes for pull-down).
        owner_loc = loc_ids[0]
        # Stage 2 (#2): ids live in the SHARED deal registry keyed by the unified
        # ExternalId (lgm_deal_<dealid>[_w<k>]) so path-2 updates the SAME record
        # instead of creating a parallel one. Id resolved local-first with a
        # Dutchie read-back fallback; a failed read-back ABORTS rather than
        # blind-creating (sub-395). The legacy submission map is kept only as a
        # migration source / for legacy deactivate.
        deal = self.deal_id
        results, failures = [], 0
        multi = len(discounts) > 1
        for discount in discounts:
            ext = discount['ExternalId']  # unified lgm_deal_<id>[_w<k>] from _dutchie_build
            tag = (ext if multi else "discount")
            existing, action = deal._dutchie_resolve_id(ext, owner_loc, lsp)
            if action == 'abort':
                failures += 1
                results.append(f"{tag}: SKIPPED — could not verify existing "
                               f"Dutchie id (read-back failed); refusing to "
                               f"blind-create a possible duplicate")
                self._deal_audit_log('publish_aborted', lsp, owner_loc, discount,
                                     error='read-back unavailable',
                                     location_restrictions=loc_ids)
                continue
            discount['ExternalId'] = ext
            discount['Id'] = existing
            discount['LocationRestrictions'] = loc_ids
            if isinstance(discount.get('DiscountMenuDisplayDetails'), dict):
                discount['DiscountMenuDisplayDetails']['DiscountId'] = existing
            payload = json.dumps({'locId': owner_loc, 'lspId': lsp, 'discount': discount}).encode()
            req = urllib.request.Request(
                f"{url}/api/admin/discounts", data=payload,
                headers={'Content-Type': 'application/json', 'x-api-key': api_key},
                method='POST')
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    raw = resp.read().decode(errors='replace')
                    rid = None
                    try:
                        parsed = json.loads(raw)
                        rid = parsed.get('discount_id') if isinstance(parsed, dict) else None
                    except (ValueError, TypeError):
                        pass
                    final_id = rid if (isinstance(rid, int) and not isinstance(rid, bool)
                                       and rid > 0) else existing
                    if final_id:
                        deal._dutchie_registry_set(ext, final_id)
                    results.append(f"{tag}: HTTP {resp.status} (1 record across "
                                   f"{len(loc_ids)} loc) {raw[:160]}")
                    self._deal_audit_log('publish', lsp, owner_loc, discount,
                                         http_status=resp.status, response=raw[:160],
                                         location_restrictions=loc_ids)
            except Exception as exc:
                failures += 1
                results.append(f"{tag}: FAILED — {exc}")
                self._deal_audit_log('publish_failed', lsp, owner_loc, discount,
                                     error=str(exc), location_restrictions=loc_ids)
        self.message_post(
            body=f"[Dutchie publish — LIVE{' — PARTIAL FAILURE' if failures else ''}]\n"
                 + f"{len(discounts)} discount(s) (one per span), each ONE record "
                   f"across {len(loc_ids)} store(s):\n"
                 + "\n".join(results)
                 + ("\nWarnings: " + "; ".join(warnings) if warnings else ''),
            message_type='comment')

    def _dutchie_deactivate(self):
        """Mark this submission's published Dutchie discounts deleted (IsDeleted=True).

        The inverse of _dutchie_publish_after_convert: expiring or revoking a
        deal in Odoo must also stop its discount in Dutchie, otherwise Dutchie
        keeps applying it until its own ValidDateTo — the publish was
        one-directional, which is why an expired deal stayed live in Dutchie.

        Only discounts we actually published are touched: the per-(span, loc)
        Dutchie ids recorded in dutchie_publish_loc_ids ({externalId:{locId:id}}).
        Nothing is created — a (span, loc) with no recorded id is skipped. We
        re-POST the SAME full payload as publish (a bare {Id, IsDeleted} body
        NREs in Dutchie's update-discount-item) with IsDeleted=True. Gated by the
        same dutchie.publish.mode param (off → no-op; dry_run → chatter only;
        live → POST). The recorded id map is left intact so a later re-publish
        UPDATES the same Dutchie record in place (reactivating it).
        """
        self.ensure_one()
        get_param = self.env['ir.config_parameter'].sudo().get_param
        mode = (get_param('dutchie.publish.mode') or 'dry_run').strip().lower()
        if mode == 'off':
            return
        try:
            published = json.loads(self.dutchie_publish_loc_ids or '{}')
        except (ValueError, TypeError):
            published = {}
        # Legacy FLAT {locId: id} maps (numeric keys) live under the lone span's
        # ExternalId. NEW consolidated maps ({externalId: id}) and legacy NESTED
        # maps ({externalId: {locId: id}}) already key by ExternalId.
        if published and all(str(k).lstrip('-').isdigit() for k in published):
            published = {f"lgm_{self.id}": published}
        # Stage 2 (#2): merge the shared deal registry (unified lgm_deal_<id>
        # keys) so discounts published via the new scheme are also pulled down.
        if self.deal_id:
            published = {**published, **self.deal_id._dutchie_registry()}
        # Anything live to pull? consolidated int id OR a non-empty legacy loc map.
        if not any((isinstance(v, int) and not isinstance(v, bool) and v)
                   or (isinstance(v, dict) and v) for v in published.values()):
            return  # never published live → nothing in Dutchie to pull

        # Rebuild the full payload so update-discount-item gets a valid Discount
        # object. Index built spans by ExternalId; fall back to the first span as
        # a template for any recorded ExternalId the rebuild no longer produces
        # (e.g. the deal's windows changed after publish).
        built = self._dutchie_build()
        lsp, discounts = built['lsp'], built['discounts']
        by_ext = {d['ExternalId']: d for d in discounts}
        template = discounts[0] if discounts else None
        loc_map_cfg = json.loads(get_param('dutchie.publish.loc_ids') or '{}')
        cfg_loc_ids = list(dict.fromkeys(
            int(x) for x in (loc_map_cfg.get(str(lsp)) or [])))

        url = api_key = None
        if mode == 'live':
            url = (get_param('dutchie.publish.url')
                   or 'https://mintinvsvc-production-6aa5.up.railway.app').rstrip('/')
            api_key = get_param('dutchie.publish.api_key')
            if not api_key:
                raise UserError("dutchie.publish.api_key is not configured for live mode.")

        results, failures, deleted = [], 0, 0

        def _send_delete(discount, post_loc, tag):
            """POST one IsDeleted payload against post_loc. Returns (ok, deleted_inc)."""
            if mode == 'dry_run':
                results.append(f"{tag}: [dry-run] would delete Dutchie id {discount['Id']}")
                self._deal_audit_log('deactivate_dry_run', lsp, int(post_loc), discount)
                return (True, 0)
            payload = json.dumps({'locId': int(post_loc), 'lspId': lsp, 'discount': discount}).encode()
            req = urllib.request.Request(
                f"{url}/api/admin/discounts", data=payload,
                headers={'Content-Type': 'application/json', 'x-api-key': api_key},
                method='POST')
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    raw = resp.read().decode(errors='replace')
                    ok = 200 <= resp.status < 300
                    results.append(f"{tag}: HTTP {resp.status} {raw[:160]}")
                    self._deal_audit_log('deactivate' if ok else 'deactivate_failed',
                                         lsp, int(post_loc), discount,
                                         http_status=resp.status, response=raw[:160])
                    return (ok, 1 if ok else 0)
            except Exception as exc:
                results.append(f"{tag}: FAILED — {exc}")
                self._deal_audit_log('deactivate_failed', lsp, int(post_loc), discount, error=str(exc))
                return (False, 0)

        for ext, recorded in published.items():
            base = dict(by_ext.get(ext) or template or {})
            if not base:
                continue
            base['ExternalId'] = ext
            base['IsDeleted'] = True
            # NEW consolidated format: one Dutchie id across all stores → one
            # IsDeleted POST under the owning loc, with every store still listed
            # in LocationRestrictions.
            if isinstance(recorded, int) and not isinstance(recorded, bool):
                if not recorded:
                    continue
                if not cfg_loc_ids:
                    results.append(f"{ext}: SKIP — no configured LocIds to delete against")
                    continue
                d = dict(base)
                d['Id'] = recorded
                d['LocationRestrictions'] = cfg_loc_ids
                if isinstance(d.get('DiscountMenuDisplayDetails'), dict):
                    d['DiscountMenuDisplayDetails'] = dict(d['DiscountMenuDisplayDetails'])
                    d['DiscountMenuDisplayDetails']['DiscountId'] = recorded
                ok, inc = _send_delete(d, cfg_loc_ids[0], f"{ext} (consolidated id {recorded})")
                failures += 0 if ok else 1
                deleted += inc
                continue
            # LEGACY per-loc format: {locId: dutchieId} — delete each separately.
            if isinstance(recorded, dict):
                for loc_id, dutchie_id in recorded.items():
                    existing = int(dutchie_id or 0)
                    if not existing:
                        continue
                    d = dict(base)
                    d['Id'] = existing
                    d['LocationRestrictions'] = []
                    if isinstance(d.get('DiscountMenuDisplayDetails'), dict):
                        d['DiscountMenuDisplayDetails'] = dict(d['DiscountMenuDisplayDetails'])
                        d['DiscountMenuDisplayDetails']['DiscountId'] = existing
                    ok, inc = _send_delete(d, int(loc_id), f"{ext} LocId {loc_id}")
                    failures += 0 if ok else 1
                    deleted += inc
        if results:
            verb = 'DRY RUN' if mode == 'dry_run' else ('LIVE' + (' — PARTIAL FAILURE' if failures else ''))
            self.message_post(
                body=f"[Dutchie deactivate — {verb}]\n" + "\n".join(results),
                message_type='comment')
        # Self-confirm: once every recorded (span, loc) is deleted in Dutchie,
        # clear the publish map so Odoo accurately reflects "no longer live in
        # Dutchie". This is the canonical signal the Deal Parity Monitor keys on
        # (expired/rejected submissions with a populated dutchie_publish_loc_ids
        # = still live). The publish/deactivate chatter retains the per-id audit
        # trail. Only on a fully-successful LIVE pull-down — partial failures
        # keep the map so the remaining ids can be retried.
        if mode == 'live' and deleted and not failures:
            self.sudo().write({'dutchie_publish_loc_ids': '{}'})
