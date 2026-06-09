# -*- coding: utf-8 -*-
"""Automated PTL plot + publish for non-AZ markets (Odoo #95041 phase 4).

Background
----------
The Daily-Deals storefront pages render from published ``mint.ptl.day`` records
(filtered by ``market_id``) and their linked ``mint.ptl.deal`` rows. Arizona is
the only market with a sustained rolling window today; it is kept populated by
operators MANUALLY plotting approved deals onto calendar days via the PTL
Calendar dragboard (``mint.ptl.deal.action_plot_windows`` ->
``mint.ptl.day.schedule_deal``) and then publishing each day. Verified live
2026-06-04: there was no automated authoring/plotting anywhere — only the
``_cron_ptl_daily_lifecycle`` publish-flip on already-plotted discounts.

This module adds an opt-in cron that sustains the rolling window automatically
per market: for each enabled market it takes that market's own approved,
date-active ``mint.ptl.deal`` rows and plots+publishes them across the next
``window_days`` days.

Safety / rollout (mirrors mint.dutchie_discount_push)
-----------------------------------------------------
* Global mode param ``mint.ptl_auto_plot.mode`` = off (default) | dry-run | live.
    - off     : the cron returns immediately. Zero behavior change on deploy.
    - dry-run : logs exactly what WOULD be plotted/published; writes nothing.
    - live    : plots + publishes.
* Per-market gate: only ``mint.region`` records with
  ``ptl_auto_plot_enabled = True`` are processed, so ops turns markets on one at
  a time once their deal content exists.
* It never authors deals and never touches Dutchie. ``action_publish``'s
  ``_push_discounts_to_dutchie`` remains independently gated (mode/markets/stores)
  and inert; this cron only drives the FE (Redis) publish path.

The cron is created programmatically in ``hooks.py`` (post_init), matching the
existing PTL lifecycle/recompute crons.
"""

import logging
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# ir.config_parameter key for the global mode switch.
AUTO_PLOT_MODE_PARAM = 'mint.ptl_auto_plot.mode'   # off | dry-run | live
# How many days forward (including today) to keep plotted, when not overridden
# by the ``mint.ptl_auto_plot.window_days`` param.
DEFAULT_WINDOW_DAYS = 14
WINDOW_DAYS_PARAM = 'mint.ptl_auto_plot.window_days'


class MintRegionAutoPlot(models.Model):
    _inherit = 'mint.region'

    ptl_auto_plot_enabled = fields.Boolean(
        string='Auto-plot Daily Deals',
        default=False,
        help='When enabled (and the global mint.ptl_auto_plot.mode is not '
             '"off"), the PTL auto-plot cron keeps this market\'s approved '
             'deals plotted and published across the rolling window. Leave off '
             'for markets whose deal content is curated manually.',
    )


class PtlDayAutoPlot(models.Model):
    _inherit = 'mint.ptl.day'

    # ── Mode + window helpers (mirror dutchie_discount_push) ─────────────────
    @api.model
    def _get_auto_plot_mode(self):
        """Return one of: 'off' (default), 'dry-run', 'live'."""
        get_param = self.env['ir.config_parameter'].sudo().get_param
        mode = (get_param(AUTO_PLOT_MODE_PARAM, 'off') or 'off').strip().lower()
        if mode not in ('off', 'dry-run', 'live'):
            _logger.warning('PTL auto-plot: unknown mode %r, treating as off', mode)
            return 'off'
        return mode

    @api.model
    def _get_auto_plot_window_days(self):
        get_param = self.env['ir.config_parameter'].sudo().get_param
        raw = get_param(WINDOW_DAYS_PARAM, str(DEFAULT_WINDOW_DAYS))
        try:
            n = int(raw)
        except (TypeError, ValueError):
            _logger.warning('PTL auto-plot: bad window_days %r, using %d',
                            raw, DEFAULT_WINDOW_DAYS)
            return DEFAULT_WINDOW_DAYS
        # Clamp to a sane range so a typo can't plot years of days.
        return max(1, min(n, 60))

    # ── Cron entry point ─────────────────────────────────────────────────────
    @api.model
    def _cron_auto_plot_publish(self):
        """Plot + publish each enabled market's approved deals over the window.

        Idempotent: schedule_deal upserts days and links deals without
        duplicates; action_publish on an already-published day is a no-op for
        the FE state but re-fires the Redis webhook (harmless refresh).
        """
        mode = self._get_auto_plot_mode()
        if mode == 'off':
            return  # zero behavior change

        window = self._get_auto_plot_window_days()
        today = fields.Date.context_today(self)
        dates = [today + timedelta(days=i) for i in range(window)]
        date_strs = [fields.Date.to_string(d) for d in dates]

        Region = self.env['mint.region'].sudo()
        Deal = self.env['mint.ptl.deal'].sudo()

        markets = Region.search([('ptl_auto_plot_enabled', '=', True)])
        if not markets:
            _logger.info('PTL auto-plot (%s): no markets enabled, nothing to do', mode)
            return

        total_plotted = 0
        total_published = 0
        for market in markets:
            # Approved deals for this market that are date-active somewhere in
            # the window. We intentionally read only state='approved' because
            # schedule_deal rejects non-approved deals (live/expired/draft).
            deals = Deal.search([
                ('market_id', '=', market.id),
                ('state', '=', 'approved'),
            ])
            if not deals:
                _logger.info('PTL auto-plot (%s): market %s has no approved deals',
                             mode, market.code or market.id)
                continue

            if mode == 'dry-run':
                _logger.info(
                    'PTL auto-plot [dry-run]: market %s would plot %d approved '
                    'deal(s) across %d day(s) %s..%s',
                    market.code or market.id, len(deals), len(date_strs),
                    date_strs[0], date_strs[-1],
                )
                continue

            # live: plot each deal across the window, collecting day ids.
            day_ids = set()
            for deal in deals:
                # Only plot dates that fall within the deal's own validity
                # range when it declares one; otherwise plot the full window.
                if deal.date_start or deal.date_end:
                    plot_dates = [
                        ds for ds, d in zip(date_strs, dates)
                        if (not deal.date_start or d >= deal.date_start)
                        and (not deal.date_end or d <= deal.date_end)
                    ]
                else:
                    plot_dates = date_strs
                if not plot_dates:
                    continue
                try:
                    ids = deal.action_plot_windows(plot_dates, market_id=market.id)
                    day_ids.update(ids)
                    total_plotted += 1
                except Exception as e:  # noqa: BLE001 - one bad deal must not abort the market
                    _logger.warning('PTL auto-plot: deal %s plot failed: %s',
                                    deal.id, e)

            # Publish the days we just plotted onto (action_publish is
            # ensure_one; iterate). Skip already-published to avoid redundant
            # webhook spam, but still publish freshly-created draft days.
            days = self.browse(sorted(day_ids)).exists()
            for day in days:
                if day.state == 'published':
                    continue
                try:
                    day.action_publish()
                    total_published += 1
                except Exception as e:  # noqa: BLE001
                    _logger.warning('PTL auto-plot: day %s publish failed: %s',
                                    day.id, e)

            _logger.info(
                'PTL auto-plot (live): market %s — %d deals plotted, %d days published',
                market.code or market.id, len(deals), len(days),
            )

        _logger.info(
            'PTL auto-plot (%s) done: %d deal-plots, %d days published across %d market(s)',
            mode, total_plotted, total_published, len(markets),
        )
