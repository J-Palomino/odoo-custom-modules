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
 * mint.deal.submission.plot_date_ids (mint.deal.submission.day: one row per
 * date). The reviewer clicks individual day cells on a month grid; each
 * selected day is a single plot_date_ids row (toggle on = create a day row,
 * toggle off = delete it). No range grouping — staging's model is already
 * one-row-per-day, the natural shape for a calendar grid.
 *
 * Days already plotted for the submission's market are shaded as context
 * (read-only mint.ptl.day lookup via plotted_dates_for_market).
 */
export class PtlDayGrid extends Component {
    static template = "mint_command_center.PtlDayGrid";
    static props = { ...standardFieldProps };

    setup() {
        this.orm = useService("orm");
        const seed = this._earliestSelected() || DateTime.now();
        this.state = useState({
            year: seed.year,
            month: seed.month, // 1-12
            plotted: new Set(), // ISO dates already on the PTL for this market
            syncing: false,
        });
        onWillStart(async () => {
            await this._loadPlotted();
        });
    }

    get list() {
        return this.props.record.data[this.props.name];
    }
    get records() {
        return this.list.records || [];
    }
    /** Selected ISO dates = one per plot_date_ids row. Derived live from the
     * bound One2many so the grid always reflects the record's true state. */
    get selectedDates() {
        const out = new Set();
        for (const rec of this.records) {
            const d = rec.data.date;
            if (d && d.toFormat) out.add(serializeDate(d));
        }
        return out;
    }
    _earliestSelected() {
        let min = null;
        for (const rec of this.records) {
            const d = rec.data.date;
            if (d && d.toFormat && (!min || d < min)) min = d;
        }
        return min;
    }
    _recordsForDate(iso) {
        return this.records.filter(
            (r) => r.data.date && r.data.date.toFormat && serializeDate(r.data.date) === iso
        );
    }

    get marketId() {
        const m = this.props.record.data.market_id;
        if (!m) return false;
        return Array.isArray(m) ? m[0] : m.id || m;
    }

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

    get monthLabel() {
        return DateTime.local(this.state.year, this.state.month, 1).toFormat("LLLL yyyy");
    }
    get weekdays() {
        return WEEKDAYS;
    }
    get weeks() {
        const first = DateTime.local(this.state.year, this.state.month, 1);
        const todayIso = serializeDate(DateTime.now());
        const selected = this.selectedDates;
        const lead = first.weekday % 7; // Sun->0 .. Sat->6
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
                    selected: selected.has(iso),
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
        return this.selectedDates.size;
    }

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

    /** Toggle a single day: delete its plot_date_ids row(s) if selected, else
     * create one. One row == one date, so no run-grouping. */
    async toggleDay(iso) {
        if (this.props.readonly || this.state.syncing) return;
        this.state.syncing = true;
        try {
            const existing = this._recordsForDate(iso);
            if (existing.length) {
                for (const rec of existing) {
                    await this.list.delete(rec);
                }
            } else {
                const rec = await this.list.addNewRecord({ position: "bottom", mode: "edit" });
                // Mark `date` dirty explicitly with a Luxon DateTime — passing it
                // via context does not reliably persist on web_save.
                await rec.update({ date: deserializeDate(iso) });
            }
        } finally {
            this.state.syncing = false;
        }
    }
    async clearAll() {
        if (this.props.readonly || this.state.syncing) return;
        this.state.syncing = true;
        try {
            for (const rec of [...this.records]) {
                await this.list.delete(rec);
            }
        } finally {
            this.state.syncing = false;
        }
    }
}

export const ptlDayGrid = {
    component: PtlDayGrid,
    displayName: _t("PTL Day Grid"),
    supportedTypes: ["one2many"],
    relatedFields: [
        { name: "id", type: "integer" },
        { name: "date", type: "date" },
    ],
};

registry.category("fields").add("ptl_day_grid", ptlDayGrid);
