/** @odoo-module **/

import { registry } from "@web/core/registry";
import { Component, useState, onWillStart } from "@odoo/owl";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { serializeDate, deserializeDate } from "@web/core/l10n/dates";

const { DateTime } = luxon;
const WEEKDAYS = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"];

/**
 * PTL day grid — click-to-toggle persistent calendar, bound to
 * mint.deal.submission.window_ids.
 *
 * Replaces the multi-window range picker (#93649 follow-up). The reviewer
 * clicks individual day cells on a month grid; the widget keeps the set of
 * selected ISO dates as local state and, on every change, re-groups the
 * contiguous runs into mint.deal.submission.window rows written back through
 * the One2many — so the already-verified all_dates() -> action_plot_windows
 * -> mint.ptl.day backend is untouched (zero schema change).
 *
 * Days already plotted for the submission's market are shaded as context
 * (read-only mint.ptl.day lookup via plotted_dates_for_market).
 */
export class PtlDayGrid extends Component {
    static template = "mint_command_center.PtlDayGrid";
    static props = { ...standardFieldProps };

    setup() {
        this.orm = useService("orm");
        // Seed the visible month from the earliest selected day, else today.
        const seed = this._earliestSelected() || DateTime.now();
        this.state = useState({
            year: seed.year,
            month: seed.month, // 1-12
            selected: this._datesFromWindows(), // Set<isoString>
            plotted: new Set(), // Set<isoString> already on the PTL for this market
            syncing: false,
            // Phase 3 full-intent (#93653): a custom range + 7 day-of-week
            // toggles drive a bulk Apply. dowMask is indexed [Sun, Mon, …,
            // Sat] to match the `weekdays` template label array; mapping
            // from luxon weekday (1=Mon..7=Sun) uses (wd % 7).
            rangeStart: null, // Luxon DateTime | null
            rangeEnd: null,   // Luxon DateTime | null
            dowMask: [true, true, true, true, true, true, true],
        });
        onWillStart(async () => {
            await this._loadPlotted();
        });
    }

    // ─── derive selection from the bound One2many ───────────────────────
    get list() {
        return this.props.record.data[this.props.name];
    }
    get records() {
        return this.list.records || [];
    }
    _datesFromWindows() {
        const out = new Set();
        for (const rec of this.records) {
            const ds = rec.data.date_start;
            const de = rec.data.date_end;
            if (!ds || !de || !ds.toFormat || de < ds) continue;
            let cur = ds;
            while (cur <= de) {
                out.add(serializeDate(cur));
                cur = cur.plus({ days: 1 });
            }
        }
        return out;
    }
    _earliestSelected() {
        let min = null;
        for (const rec of this.records) {
            const ds = rec.data.date_start;
            if (ds && ds.toFormat && (!min || ds < min)) min = ds;
        }
        return min;
    }

    get marketId() {
        const m = this.props.record.data.market_id;
        if (!m) return false;
        return Array.isArray(m) ? m[0] : m.id || m;
    }

    // ─── market context shading ─────────────────────────────────────────
    async _loadPlotted() {
        const mid = this.marketId;
        if (!mid) {
            this.state.plotted = new Set();
            return;
        }
        const first = DateTime.local(this.state.year, this.state.month, 1);
        const last = first.endOf("month");
        const dates = await this.orm.call(
            "mint.ptl.day",
            "plotted_dates_for_market",
            [mid, serializeDate(first), serializeDate(last)]
        );
        this.state.plotted = new Set(dates || []);
    }

    // ─── month grid model ───────────────────────────────────────────────
    get monthLabel() {
        return DateTime.local(this.state.year, this.state.month, 1).toFormat("LLLL yyyy");
    }
    get weekdays() {
        return WEEKDAYS;
    }
    /** 6 rows x 7 cols of cells {iso, day, inMonth, selected, plotted, today}. */
    get weeks() {
        const first = DateTime.local(this.state.year, this.state.month, 1);
        const todayIso = serializeDate(DateTime.now());
        // luxon weekday: 1=Mon..7=Sun; grid starts Sunday.
        const lead = first.weekday % 7; // Sun->0, Mon->1, ... Sat->6
        const start = first.minus({ days: lead });
        const weeks = [];
        let cur = start;
        for (let w = 0; w < 6; w++) {
            const row = [];
            for (let d = 0; d < 7; d++) {
                const iso = serializeDate(cur);
                row.push({
                    iso,
                    day: cur.day,
                    inMonth: cur.month === this.state.month,
                    selected: this.state.selected.has(iso),
                    plotted: this.state.plotted.has(iso),
                    today: iso === todayIso,
                });
                cur = cur.plus({ days: 1 });
            }
            weeks.push(row);
        }
        return weeks;
    }
    get dayCount() {
        return this.state.selected.size;
    }

    // ─── navigation ─────────────────────────────────────────────────────
    async prevMonth() {
        const d = DateTime.local(this.state.year, this.state.month, 1).minus({ months: 1 });
        this.state.year = d.year;
        this.state.month = d.month;
        await this._loadPlotted();
    }
    async nextMonth() {
        const d = DateTime.local(this.state.year, this.state.month, 1).plus({ months: 1 });
        this.state.year = d.year;
        this.state.month = d.month;
        await this._loadPlotted();
    }

    // ─── toggle + persist ───────────────────────────────────────────────
    async toggleDay(iso) {
        if (this.props.readonly || this.state.syncing) return;
        if (this.state.selected.has(iso)) {
            this.state.selected.delete(iso);
        } else {
            this.state.selected.add(iso);
        }
        await this._syncWindows();
    }
    async clearAll() {
        if (this.props.readonly || this.state.syncing) return;
        this.state.selected = new Set();
        await this._syncWindows();
    }

    // ─── quick fill (Phase 3, #93653) ───────────────────────────────────
    // Each button ADDS to the current selection (non-destructive) so a user
    // composing across months can stack picks without losing prior work.
    // Use Clear to reset.
    async setEdlpMonth() { await this._fillMonth(() => true); }
    async setWeekdaysMonth() { await this._fillMonth((wd) => wd >= 1 && wd <= 5); }
    async setWeekendsMonth() { await this._fillMonth((wd) => wd === 6 || wd === 7); }

    /** Add every day in the currently-visible month whose luxon weekday
     * (1=Mon..7=Sun) matches `predicate(wd)` to the selection. */
    async _fillMonth(predicate) {
        if (this.props.readonly || this.state.syncing) return;
        const first = DateTime.local(this.state.year, this.state.month, 1);
        const last = first.endOf("month");
        const sel = new Set(this.state.selected);
        let cur = first;
        while (cur <= last) {
            if (predicate(cur.weekday)) sel.add(serializeDate(cur));
            cur = cur.plus({ days: 1 });
        }
        this.state.selected = sel;
        await this._syncWindows();
    }

    // ─── range + DOW toggles (Phase 3 full-intent, #93653) ──────────────
    // Inputs render local-zone dates; serialize/deserialize bridges between
    // the HTML <input type="date"> string and the Luxon DateTime we store.
    get rangeStartIso() {
        return this.state.rangeStart ? serializeDate(this.state.rangeStart) : "";
    }
    get rangeEndIso() {
        return this.state.rangeEnd ? serializeDate(this.state.rangeEnd) : "";
    }
    onRangeStartChange(ev) {
        const v = ev.target.value;
        this.state.rangeStart = v ? deserializeDate(v) : null;
    }
    onRangeEndChange(ev) {
        const v = ev.target.value;
        this.state.rangeEnd = v ? deserializeDate(v) : null;
    }

    /** Flip one DOW toggle on/off. idx 0=Sun, 1=Mon, …, 6=Sat. */
    toggleDow(idx) {
        if (this.props.readonly) return;
        this.state.dowMask = this.state.dowMask.map((v, i) => (i === idx ? !v : v));
    }
    /** Preset: every day. Activates all 7 toggles — this is the literal
     * "click Every Day → all 7 day-of-week toggles activate" from the AC. */
    setEdlpDows() {
        if (this.props.readonly) return;
        this.state.dowMask = [true, true, true, true, true, true, true];
    }
    setWeekdaysDows() {
        if (this.props.readonly) return;
        // [Sun, Mon, Tue, Wed, Thu, Fri, Sat] → only Mon-Fri on
        this.state.dowMask = [false, true, true, true, true, true, false];
    }
    setWeekendsDows() {
        if (this.props.readonly) return;
        this.state.dowMask = [true, false, false, false, false, false, true];
    }
    get hasValidRange() {
        return !!(this.state.rangeStart && this.state.rangeEnd
            && this.state.rangeEnd >= this.state.rangeStart);
    }
    /** Bulk-fill: every date in [rangeStart, rangeEnd] whose DOW is active
     * gets added to the selection. Additive (composes with existing picks);
     * use Clear to reset. */
    async applyRange() {
        if (this.props.readonly || this.state.syncing) return;
        if (!this.hasValidRange) return;
        const sel = new Set(this.state.selected);
        let cur = this.state.rangeStart;
        while (cur <= this.state.rangeEnd) {
            // luxon: Mon=1..Sun=7 — map to dowMask index Sun=0..Sat=6 via %7
            const idx = cur.weekday % 7;
            if (this.state.dowMask[idx]) sel.add(serializeDate(cur));
            cur = cur.plus({ days: 1 });
        }
        this.state.selected = sel;
        await this._syncWindows();
    }

    /** Collapse the selected ISO dates into contiguous runs and rewrite the
     * One2many. Delete-all + recreate is simple and correct; window counts are
     * small (a handful) so the churn is negligible and only commits on save. */
    async _syncWindows() {
        this.state.syncing = true;
        try {
            const runs = this._computeRuns([...this.state.selected].sort());
            for (const rec of [...this.records]) {
                await this.list.delete(rec);
            }
            let seq = 10;
            for (const run of runs) {
                const rec = await this.list.addNewRecord({ position: "bottom", mode: "edit" });
                // Mark date_start/date_end dirty explicitly (Luxon DateTime) —
                // passing them via context does not reliably persist (same
                // gotcha the range picker hit on web_save).
                await rec.update({
                    sequence: seq,
                    date_start: run.start,
                    date_end: run.end,
                });
                seq += 10;
            }
        } finally {
            this.state.syncing = false;
        }
    }

    /** sortedIso -> [{start: DateTime, end: DateTime}] contiguous runs. */
    _computeRuns(sortedIso) {
        const runs = [];
        let runStart = null;
        let prev = null;
        for (const iso of sortedIso) {
            const dt = deserializeDate(iso);
            if (runStart === null) {
                runStart = dt;
                prev = dt;
                continue;
            }
            if (dt.toMillis() === prev.plus({ days: 1 }).toMillis()) {
                prev = dt;
            } else {
                runs.push({ start: runStart, end: prev });
                runStart = dt;
                prev = dt;
            }
        }
        if (runStart !== null) runs.push({ start: runStart, end: prev });
        return runs;
    }
}

export const ptlDayGrid = {
    component: PtlDayGrid,
    displayName: _t("PTL Day Grid"),
    supportedTypes: ["one2many"],
    relatedFields: [
        { name: "id", type: "integer" },
        { name: "sequence", type: "integer" },
        { name: "date_start", type: "date" },
        { name: "date_end", type: "date" },
        { name: "day_count", type: "integer" },
    ],
};

registry.category("fields").add("ptl_day_grid", ptlDayGrid);
