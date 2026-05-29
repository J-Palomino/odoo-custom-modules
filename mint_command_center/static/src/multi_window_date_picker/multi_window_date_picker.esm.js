/** @odoo-module **/

import { registry } from "@web/core/registry";
import { Component } from "@odoo/owl";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { _t } from "@web/core/l10n/translation";
import { serializeDate, deserializeDate } from "@web/core/l10n/dates";

const { DateTime } = luxon;

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
        return this.list.records || [];
    }

    /** Total number of distinct days across the union of all windows.
     * Iterates via Luxon's .plus({days: 1}) to stay in the field's local
     * calendar — sidesteps the JS Date / .toISOString() UTC drift that
     * misdedupes ranges for users east of UTC. */
    get dayCount() {
        const seen = new Set();
        for (const rec of this.records) {
            const ds = rec.data.date_start;
            const de = rec.data.date_end;
            if (!ds || !de || !ds.toFormat || de < ds) continue;
            let cur = ds;
            while (cur <= de) {
                seen.add(serializeDate(cur));
                cur = cur.plus({ days: 1 });
            }
        }
        return seen.size;
    }

    get windowCount() {
        return this.records.length;
    }

    /** ISO date (YYYY-MM-DD) for HTML <input type="date">. Date fields
     * always materialize as Luxon DateTime in the OWL record layer. */
    isoFor(field, rec) {
        const v = rec.data[field];
        return v ? serializeDate(v) : "";
    }

    async addWindow() {
        const maxSeq = this.records.reduce(
            (m, r) => Math.max(m, r.data.sequence || 0), 0
        );
        // Default new window to (last_end + 1 day) for natural composition;
        // fall back to today when there's no prior window. Keep it as a Luxon
        // DateTime so we can write it through record.update() below.
        let defaultStart;
        const lastRec = this.records[this.records.length - 1];
        if (lastRec && lastRec.data.date_end && lastRec.data.date_end.plus) {
            defaultStart = lastRec.data.date_end.plus({ days: 1 });
        } else {
            // DateTime.now() resolves "today" in the user's local zone; the
            // old new Date().toISOString().slice(0,10) used the UTC date, which
            // seeds tomorrow for users west of UTC late in the day — the exact
            // drift dayCount() avoids.
            defaultStart = DateTime.now();
        }
        // Passing default_* through addNewRecord's context does NOT reliably
        // mark date_start/date_end dirty on the virtual record, so web_save
        // omits them and the server rejects the create ("Missing required
        // value for 'Start Date'"). Set them explicitly via update() — the same
        // reason onDateChange must hand record.update a Luxon DateTime, not a
        // raw string.
        const rec =
            (await this.list.addNewRecord({ position: "bottom", mode: "edit" })) ||
            this.records[this.records.length - 1];
        await rec.update({
            sequence: maxSeq + 10,
            date_start: defaultStart,
            date_end: defaultStart,
        });
    }

    async removeWindow(rec) {
        await this.list.delete(rec);
    }

    async onDateChange(rec, fieldName, ev) {
        // The <input type="date"> yields a "yyyy-MM-dd" string, but record.update
        // for a Date field expects a Luxon DateTime — passing the raw string
        // silently no-ops and the row keeps its default value. Deserialize first.
        const raw = ev.target.value;
        const v = raw ? deserializeDate(raw) : false;
        await rec.update({ [fieldName]: v });
    }
}

export const multiWindowDatePicker = {
    component: MultiWindowDatePicker,
    displayName: _t("Multi-Window Date Picker"),
    supportedTypes: ["one2many"],
    // Eagerly load these child fields — without relatedFields AND without an
    // inner <list> in the form view, the relational model only loads id +
    // display_name, leaving rec.data.date_start etc. undefined at render.
    // Precedent: base_tier_validation/.../tier_review_widget.esm.js:39-50.
    relatedFields: [
        { name: "id",         type: "integer" },
        { name: "sequence",   type: "integer" },
        { name: "date_start", type: "date" },
        { name: "date_end",   type: "date" },
        { name: "day_count",  type: "integer" },
    ],
};

registry.category("fields").add("multi_window_date_picker", multiWindowDatePicker);
