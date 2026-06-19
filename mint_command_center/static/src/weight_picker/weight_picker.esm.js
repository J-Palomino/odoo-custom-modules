/** @odoo-module **/

import { registry } from "@web/core/registry";
import { Component, useState } from "@odoo/owl";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { Dialog } from "@web/core/dialog/dialog";
import { CheckBox } from "@web/core/checkbox/checkbox";

/**
 * Weight picker (#deal-submission Inclusions tab).
 *
 * Sibling of the Specific Products picker, but for the discrete weight filter.
 * The reviewer picks from the REAL in-stock sizes (parsed from the candidate
 * products by the model) the same way they pick brands/categories: add one at
 * a time as removable chips, or mass-clear them all at once.
 *
 * The field is a plain JSON list of canonical weight keys (e.g.
 * ["0.5:g", "3.5:g"]). The available options — {key, label, count} — are read
 * from the companion `available_weight_options` JSON field the view computes
 * (distinct in-stock sizes, before any weight filter). No ORM call: the option
 * list lives entirely on the record, so the browse dialog is purely in-memory.
 */

class WeightPickerDialog extends Component {
    static template = "mint_command_center.WeightPickerDialog";
    static components = { Dialog, CheckBox };
    static props = {
        title: String,
        options: Array, // [{key, label, count}]
        selectedKeys: { type: Array, optional: true },
        onConfirm: Function,
        close: Function, // injected by the dialog service
    };

    setup() {
        this.state = useState({
            search: "",
            // Persistent selection held as a Set, mirroring the products picker.
            selected: new Set(this.props.selectedKeys || []),
        });
    }

    get filteredOptions() {
        const s = (this.state.search || "").trim().toLowerCase();
        if (!s) {
            return this.props.options;
        }
        return this.props.options.filter((o) =>
            (o.label || "").toLowerCase().includes(s)
        );
    }

    onSearchInput(ev) {
        this.state.search = ev.target.value;
    }

    // ─── selection (component-state Set) ────────────────────────────────────
    isSelected(key) {
        return this.state.selected.has(key);
    }
    toggle(key) {
        if (this.state.selected.has(key)) {
            this.state.selected.delete(key);
        } else {
            this.state.selected.add(key);
        }
    }
    get allShownSelected() {
        const opts = this.filteredOptions;
        return opts.length > 0 && opts.every((o) => this.state.selected.has(o.key));
    }
    get someShownSelected() {
        return this.filteredOptions.some((o) => this.state.selected.has(o.key));
    }
    toggleSelectShown() {
        const keys = this.filteredOptions.map((o) => o.key);
        if (this.allShownSelected) {
            keys.forEach((k) => this.state.selected.delete(k));
        } else {
            keys.forEach((k) => this.state.selected.add(k));
        }
    }
    clearSelection() {
        this.state.selected = new Set();
    }

    confirm() {
        this.props.onConfirm([...this.state.selected]);
        this.props.close();
    }
    discard() {
        this.props.close();
    }
}

export class WeightPickerField extends Component {
    static template = "mint_command_center.WeightPickerField";
    static props = {
        ...standardFieldProps,
        optionsField: { type: String, optional: true },
    };

    setup() {
        this.dialog = useService("dialog");
    }

    /** Available weight options [{key, label, count}] from the companion field. */
    get options() {
        const field = this.props.optionsField || "available_weight_options";
        return this.props.record.data[field] || [];
    }

    /** Currently-selected keys (the JSON value of this field). */
    get selectedKeys() {
        return this.props.record.data[this.props.name] || [];
    }

    /** Labelled chips for the selected keys; falls back to the key when an
     *  option is no longer offered (e.g. stock changed since the pick). */
    get tags() {
        const byKey = {};
        for (const o of this.options) {
            byKey[o.key] = o.label;
        }
        return this.selectedKeys.map((key) => ({
            key,
            text: byKey[key] || key.replace(":", ""),
        }));
    }

    async update(keys) {
        await this.props.record.update({ [this.props.name]: keys });
    }

    openPicker() {
        if (this.props.readonly) {
            return;
        }
        this.dialog.add(WeightPickerDialog, {
            title: _t("Select Weights"),
            options: this.options,
            selectedKeys: [...this.selectedKeys],
            onConfirm: (keys) => this.update(keys),
        });
    }

    async removeOne(key) {
        if (this.props.readonly) {
            return;
        }
        await this.update(this.selectedKeys.filter((k) => k !== key));
    }

    async clearAll() {
        if (this.props.readonly) {
            return;
        }
        if (this.selectedKeys.length) {
            await this.update([]);
        }
    }
}

export const weightPickerField = {
    component: WeightPickerField,
    displayName: _t("Weight Picker"),
    supportedTypes: ["json"],
    extractProps: ({ options }) => ({
        optionsField: options.optionsField,
    }),
};

registry.category("fields").add("mdcc_weight_picker", weightPickerField);
