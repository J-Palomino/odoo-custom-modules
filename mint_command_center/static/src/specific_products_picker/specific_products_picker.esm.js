/** @odoo-module **/

import { registry } from "@web/core/registry";
import { Component, useState, onWillStart } from "@odoo/owl";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { Dialog } from "@web/core/dialog/dialog";
import { Pager } from "@web/core/pager/pager";
import { CheckBox } from "@web/core/checkbox/checkbox";

/**
 * Specific-Products picker (#deal-submission Inclusions tab).
 *
 * Replaces the stock many2many_tags "Search More…" SelectCreateDialog for the
 * `product_ids` field. Two UX fixes vendors asked for:
 *
 *   1. Selection persists across pagination. The stock dialog keeps its
 *      checkbox selection on the *list model*, which is reloaded (and wiped)
 *      every time the pager offset changes — so checking page 1, paging to
 *      page 2 and back lost page 1. Here the selection is a plain Set held in
 *      component state; paging only reloads the *visible rows*, never the Set.
 *   2. Mass clear. A single "Clear all" on the field (and "Clear selection"
 *      inside the dialog) empties the picks at once instead of removing tags
 *      one X at a time.
 *
 * The picker scope (which products are offered) is read straight from the
 * companion `available_product_ids` field the view already computes
 * (brand + picked categories + market stores, in stock) — the same scope the
 * stock domain `[('id','in', available_product_ids)]` enforced.
 */

class ProductPickerDialog extends Component {
    static template = "mint_command_center.ProductPickerDialog";
    static components = { Dialog, Pager, CheckBox };
    static props = {
        title: String,
        resModel: String,
        domain: { type: Array, optional: true },
        context: { type: Object, optional: true },
        selectedIds: { type: Array, optional: true },
        onConfirm: Function,
        close: Function, // injected by the dialog service
    };

    setup() {
        this.orm = useService("orm");
        this.state = useState({
            search: "",
            offset: 0,
            limit: 20,
            total: 0,
            records: [],
            loading: false,
            // Persistent selection — survives every pager reload below.
            selected: new Set(this.props.selectedIds || []),
        });
        onWillStart(() => this.load());
    }

    get domain() {
        const base = (this.props.domain || []).slice();
        const s = (this.state.search || "").trim();
        if (!s) {
            return base;
        }
        // AND(scope, OR(name ilike, default_code ilike))
        return base.concat(["|", ["name", "ilike", s], ["default_code", "ilike", s]]);
    }

    async load() {
        this.state.loading = true;
        try {
            const ctx = this.props.context || {};
            const domain = this.domain;
            const [total, records] = await Promise.all([
                // search_count only accepts (domain, limit) server-side — never
                // pass offset/order here or it raises a TypeError.
                this.orm.searchCount(this.props.resModel, domain, { context: ctx }),
                this.orm.searchRead(
                    this.props.resModel,
                    domain,
                    ["display_name", "default_code"],
                    {
                        limit: this.state.limit,
                        offset: this.state.offset,
                        order: "display_name",
                        context: ctx,
                    }
                ),
            ]);
            this.state.total = total;
            this.state.records = records;
        } finally {
            this.state.loading = false;
        }
    }

    onSearchInput(ev) {
        const value = ev.target.value;
        clearTimeout(this._searchTimer);
        this._searchTimer = setTimeout(() => {
            this.state.search = value;
            this.state.offset = 0;
            this.load();
        }, 300);
    }

    async onPager({ offset, limit }) {
        this.state.offset = offset;
        this.state.limit = limit;
        await this.load();
    }

    // ─── selection (component-state Set — pager-proof) ──────────────────────
    isSelected(id) {
        return this.state.selected.has(id);
    }
    toggle(id) {
        if (this.state.selected.has(id)) {
            this.state.selected.delete(id);
        } else {
            this.state.selected.add(id);
        }
    }
    get allOnPageSelected() {
        return (
            this.state.records.length > 0 &&
            this.state.records.every((r) => this.state.selected.has(r.id))
        );
    }
    get someOnPageSelected() {
        return this.state.records.some((r) => this.state.selected.has(r.id));
    }
    toggleSelectPage() {
        const ids = this.state.records.map((r) => r.id);
        if (this.allOnPageSelected) {
            ids.forEach((id) => this.state.selected.delete(id));
        } else {
            ids.forEach((id) => this.state.selected.add(id));
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

export class SpecificProductsField extends Component {
    static template = "mint_command_center.SpecificProductsField";
    static props = {
        ...standardFieldProps,
        scopeField: { type: String, optional: true },
    };

    setup() {
        this.dialog = useService("dialog");
    }

    get list() {
        return this.props.record.data[this.props.name];
    }
    get relation() {
        return this.props.record.fields[this.props.name].relation;
    }
    get tags() {
        return (this.list.records || []).map((rec) => ({
            id: rec.id,
            resId: rec.resId,
            text: rec.data.display_name || rec.data.name || `#${rec.resId}`,
        }));
    }

    /** Allowed-product ids from the companion compute field the view declares. */
    get scopeIds() {
        const field = this.props.scopeField || "available_product_ids";
        const data = this.props.record.data[field];
        if (data && data.currentIds) {
            return [...data.currentIds];
        }
        return null;
    }

    openPicker() {
        if (this.props.readonly) {
            return;
        }
        const scopeIds = this.scopeIds;
        this.dialog.add(ProductPickerDialog, {
            title: _t("Select Specific Products"),
            resModel: this.relation,
            domain: scopeIds ? [["id", "in", scopeIds]] : [],
            context: this.props.context,
            selectedIds: [...this.list.currentIds],
            onConfirm: (ids) => this.applySelection(ids),
        });
    }

    async applySelection(ids) {
        const current = this.list.currentIds;
        const currentSet = new Set(current);
        const desiredSet = new Set(ids);
        const add = ids.filter((id) => !currentSet.has(id));
        const remove = current.filter((id) => !desiredSet.has(id));
        if (add.length || remove.length) {
            await this.list.addAndRemove({ add, remove });
        }
    }

    async removeOne(resId) {
        if (this.props.readonly) {
            return;
        }
        await this.list.addAndRemove({ remove: [resId] });
    }

    async clearAll() {
        if (this.props.readonly) {
            return;
        }
        const remove = [...this.list.currentIds];
        if (remove.length) {
            await this.list.addAndRemove({ remove });
        }
    }
}

export const specificProductsField = {
    component: SpecificProductsField,
    displayName: _t("Specific Products Picker"),
    supportedTypes: ["many2many"],
    relatedFields: [{ name: "display_name", type: "char" }],
    extractProps: ({ options }) => ({
        scopeField: options.scopeField,
    }),
};

registry.category("fields").add("mdcc_specific_products", specificProductsField);
