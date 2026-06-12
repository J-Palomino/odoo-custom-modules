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
    live     — POST per target LocId (requires ``dutchie.publish.api_key``).

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

from odoo import api, models
from odoo.exceptions import UserError

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
        mode = (self.env['ir.config_parameter'].sudo()
                .get_param('dutchie.publish.mode') or 'dry_run').strip().lower()
        # #2 reviewer gate: a deal can't convert (and thus can't publish) with
        # no numeric value. Exemptions: bogo/clearance (valid without one) and
        # mode=off (publishing disabled — don't block legacy conversions).
        if (mode != 'off' and not self.discount_value
                and self.discount_type not in ('bogo', 'clearance')):
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

        cat_id = None
        if self.product_category and self.product_category.strip().lower() not in NOISE:
            cat = self.env['product.category'].search(
                [('name', 'ilike', self.product_category.strip())], limit=1)
            if cat and str(cat.dutchie_category_id or '').strip().isdigit():
                cat_id = int(cat.dutchie_category_id)

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
        dates = sorted(dates)
        dows = {d.weekday() for d in dates}  # Mon=0..Sun=6
        # python weekday Mon=0; DAY_KEYS starts Sunday
        day_flags = {}
        for idx, key in enumerate(DAY_KEYS):  # Sunday=0 in Dutchie ordering
            py = (idx - 1) % 7                # Sunday -> 6, Monday -> 0 ...
            day_flags[key] = py in dows

        calc = CALC_BY_TYPE.get(self.discount_type)
        if not calc:
            if self.discount_type == 'bogo':
                # BOGO publishes as 100% off with item threshold 2 by Dutchie
                # convention (percent-100); keep manual until verified.
                raise UserError("BOGO auto-publish is not enabled yet — build it in Backoffice.")
            raise UserError(f"Unsupported discount type {self.discount_type!r} for Dutchie publish.")
        value = float(self.discount_value or 0)
        threshold_min = None
        if calc == 2:
            # Model convention is WHOLE percents (50 = 50% — see
            # _format_sales_details). Divide unconditionally: a stored 1
            # means 1% (0.01), never 100%.
            value = value / 100.0
        elif calc == 6:
            m = RE_NFOR.search(self.sales_details or '')
            threshold_min = int(m.group(1)) if m else 2

        restrictions = {k: {'IsExclusion': False, 'RestrictionIds': []}
                        for k in ('Strain', 'Weight', 'Category', 'Tag', 'InventoryTag',
                                  'Tier', 'Brand', 'Vendor', 'Product')}
        if brand_ids:
            restrictions['Brand'] = {'IsExclusion': False, 'RestrictionIds': brand_ids}
        elif exc_brand_ids:
            restrictions['Brand'] = {'IsExclusion': True, 'RestrictionIds': exc_brand_ids}
        if prod_inc:
            restrictions['Product'] = {'IsExclusion': False, 'RestrictionIds': prod_inc}
            if prod_exc:
                warnings.append("product exclusions dropped (Product slot used by includes)")
        elif prod_exc:
            restrictions['Product'] = {'IsExclusion': True, 'RestrictionIds': prod_exc}
        if cat_id:
            restrictions['Category'] = {'IsExclusion': False, 'RestrictionIds': [cat_id]}

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

        label = (f"{value * 100:g}% Off" if calc == 2
                 else f"{threshold_min} for ${value:g}" if calc == 6
                 else f"${value:g} Off" if calc == 1
                 else f"${value:g}")
        discount = {
            'Id': 0,
            'ApplicationMethodId': 1,
            'CanStackAutomatically': False,
            'Constraints': [],
            'DiscountDescription': f"lgm | {self.vendor_name} (Odoo sub {self.id})",
            'ExternalId': f"lgm_{self.id}",
            'FirstTimeCustomerOnly': 0,
            'IgnoreNetTax': False,
            'IsAvailableOnline': True,
            'IsBundledDiscount': calc == 6,
            'LocationRestrictions': [],
            'OnlineName': f"{self.vendor_name} — {label}",
            'PaymentRestrictions': {'PayByBankSignupIncentive': False},
            'RedemptionLimit': '',
            'RequireManagerApproval': False,
            'RestrictToGroupIds': [],
            'RestrictToSegmentIds': [],
            'PlatformTypeRestrictions': [{'PlatformTypeId': 2, 'IsExclusion': False}],
            'OrderTypeRestrictions': [],
            'Reward': {
                'DiscountRewardId': None,
                'HasThreshold': calc in (5, 6),
                'ApplyToOnlyOneItem': False,
                'CalculationMethodId': calc,
                'DiscountValue': value,
                'IncludeNonCannabis': False,
                'ItemGroupTypeId': 6,
                'ManualDefaultApplyTo': 1,
                'Restrictions': restrictions,
                'ThresholdMax': None,
                'ThresholdMin': threshold_min,
                'ThresholdTypeId': 1 if calc == 6 else 2 if calc == 5 else 0,
            },
            'SavedWithAdvancedOptions': False,
            'ValidDateFrom': self._dutchie_date(dates[0]),
            'ValidDateTo': self._dutchie_date(dates[-1], end_of_day=True),
            'DiscountCode': '',
            'MaxRedemptions': 0,
            'RedemptionLimitCountingMode': 0,
            **day_flags,
            'MenuDisplayRank': 0,
            'DiscountMenuDisplayDetails': {
                'DiscountId': 0,
                'MenuDisplayImageUrl': '',
                'MenuDisplayName': self.vendor_name or '',
                'MenuDisplayDescription': '',
                'DiscountMenuDisplayId': None,
            },
        }
        return {'lsp': lsp, 'discount': discount, 'warnings': warnings}

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _dutchie_publish_after_convert(self):
        self.ensure_one()
        get_param = self.env['ir.config_parameter'].sudo().get_param
        mode = (get_param('dutchie.publish.mode') or 'dry_run').strip().lower()
        if mode == 'off':
            return
        built = self._dutchie_build()
        lsp, discount, warnings = built['lsp'], built['discount'], built['warnings']
        loc_map = json.loads(get_param('dutchie.publish.loc_ids') or '{}')
        loc_ids = loc_map.get(str(lsp)) or []

        if mode == 'dry_run':
            body = (
                f"[Dutchie publish — DRY RUN]\n"
                f"LSP {lsp} | target LocIds {loc_ids or '(none configured)'}\n"
                f"Warnings: {'; '.join(warnings) or 'none'}\n"
                f"Payload:\n{json.dumps(discount, indent=1)}"
            )
            self.message_post(body=body, message_type='comment')
            return

        # live
        url = (get_param('dutchie.publish.url')
               or 'https://mintinvsvc-production-6aa5.up.railway.app').rstrip('/')
        api_key = get_param('dutchie.publish.api_key')
        if not api_key:
            raise UserError("dutchie.publish.api_key is not configured for live mode.")
        if not loc_ids:
            raise UserError(f"dutchie.publish.loc_ids has no LocIds for LSP {lsp}.")
        # Per-LocId isolation: one store failing must not hide which stores
        # DID publish — accumulate every outcome and report them all.
        results, failures = [], 0
        for loc_id in loc_ids:
            payload = json.dumps({'locId': loc_id, 'lspId': lsp, 'discount': discount}).encode()
            req = urllib.request.Request(
                f"{url}/api/admin/discounts", data=payload,
                headers={'Content-Type': 'application/json', 'x-api-key': api_key},
                method='POST')
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    results.append(f"LocId {loc_id}: HTTP {resp.status} "
                                   f"{resp.read()[:200].decode(errors='replace')}")
            except Exception as exc:
                failures += 1
                results.append(f"LocId {loc_id}: FAILED — {exc}")
        self.message_post(
            body=f"[Dutchie publish — LIVE{' — PARTIAL FAILURE' if failures else ''}]\n"
                 + "\n".join(results)
                 + ("\nWarnings: " + "; ".join(warnings) if warnings else ''),
            message_type='comment')
