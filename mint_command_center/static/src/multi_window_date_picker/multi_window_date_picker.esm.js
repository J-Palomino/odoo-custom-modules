/** @odoo-module **/

import { registry } from "@web/core/registry";
import { Component } from "@odoo/owl";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { _t } from "@web/core/l10n/translation";

/**
 * Multi-window date picker — bound to mint.deal.submission.window_ids.
 *
 * Renders N rows of (start, end, delete) for plotting a deal across
 * non-contiguous date windows. A "+ Add Window" button appends a new row;
 * a live counter shows the total number of distinct days the union covers.
 *
 * Phase 3 will extend this widget with EDLP / Weekdays Only / Weekends Only
 * quick-button row and the seven Sun-Sat manual toggles. For Phase 2 it just
 * does the window CRUD.
 */
export class MultiWindowDatePicker extends Component {
    static template = "mint_command_center.MultiWindowDatePicker";
    static props = { ...standardFieldProps };

    get list() {
        return this.props.record.data[this.props.name];
    }

    get records() {
        // StaticList.records is the canonical accessor in Odoo 19.
        return this.list.records || [];
    }

    /** Total number of distinct days across the union of all windows. */
    get dayCount() {
        const seen = new Set();
        for (const rec of this.records) {
            const ds = rec.data.date_start;
            const de = rec.data.date_end;
            if (!ds || !de) continue;
            // Odoo passes Luxon DateTime objects for Date fields; tolerate
            // both Luxon and ISO-string shapes.
            const start = ds.toJSDate ? ds.toJSDate() : new Date(ds);
            const end = de.toJSDate ? de.toJSDate() : new Date(de);
            if (isNaN(start.getTime()) || isNaN(end.getTime()) || end < start) continue;
            const cur = new Date(start.getFullYear(), start.getMonth(), start.getDate());
            const last = new Date(end.getFullYear(), end.getMonth(), end.getDate());
            while (cur <= last) {
                seen.add(cur.toISOString().slice(0, 10));
                cur.setDate(cur.getDate() + 1);
            }
        }
        return seen.size;
    }

    get windowCount() {
        return this.records.length;
    }

    /** ISO date (YYYY-MM-DD) suitable for an HTML <input type="date"> value. */
    isoFor(field, rec) {
        const v = rec.data[field];
        if (!v) return "";
        if (v.toISODate) return v.toISODate();  // Luxon
        if (typeof v === "string") return v.slice(0, 10);
        return "";
    }

    async addWindow() {
        // Default new window to "today..today" — the user immediately picks
        // real dates. Sequence trails the highest existing seq by +10.
        const maxSeq = this.records.reduce(
            (m, r) => Math.max(m, r.data.sequence || 0), 0
        );
        const today = new Date().toISOString().slice(0, 10);
        await this.list.addNewRecord({
            position: "bottom",
            context: {
                default_sequence: maxSeq + 10,
                default_date_start: today,
                default_date_end: today,
            },
            mode: "edit",
        });
    }

    async removeWindow(rec) {
        await this.list.delete(rec);
    }

    async onDateChange(rec, fieldName, ev) {
        const v = ev.target.value || false;
        await rec.update({ [fieldName]: v });
    }
}

export const multiWindowDatePicker = {
    component: MultiWindowDatePicker,
    displayName: _t("Multi-Window Date Picker"),
    supportedTypes: ["one2many"],
};

registry.category("fields").add("multi_window_date_picker", multiWindowDatePicker);
