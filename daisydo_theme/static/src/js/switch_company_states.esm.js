/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import { session } from "@web/session";
import { _t } from "@web/core/l10n/translation";
import { SwitchCompanyMenu } from "@web/webclient/switch_company_menu/switch_company_menu";

/**
 * Group the multi-company switcher list by US state.
 *
 * Odoo's base `computeVisibleCompanies()` returns a flat, hierarchy-ordered
 * list of `{ company, level }` entries. We keep those entries (so selectAll /
 * search / toggle behaviour is untouched) but annotate each with `stateName`
 * and reorder them into state buckets. The template
 * (daisydo_theme.SwitchCompanyMenuItems) renders a subheader whenever the
 * state changes between two consecutive rows.
 *
 * State names come from `session.company_states` (injected in
 * daisydo_theme/models/ir_http.py). Companies without a state — the root
 * "Mint Cannabis" company and the corporate entities — fall into an "Other"
 * group, sorted last. Display-only: the Odoo company tree is unchanged.
 */
patch(SwitchCompanyMenu.prototype, {
    computeVisibleCompanies() {
        const entries = super.computeVisibleCompanies();
        const states = session.company_states || {};

        const OTHER = _t("Other");
        for (const entry of entries) {
            entry.stateName = states[entry.company.id] || OTHER;
        }

        // Stable sort: group by state, "Other" last, original order within a group.
        return entries
            .map((entry, index) => ({ entry, index }))
            .sort((a, b) => {
                const sa = a.entry.stateName;
                const sb = b.entry.stateName;
                if (sa === sb) {
                    return a.index - b.index;
                }
                if (sa === OTHER) {
                    return 1;
                }
                if (sb === OTHER) {
                    return -1;
                }
                return sa.localeCompare(sb);
            })
            .map((wrapped) => wrapped.entry);
    },
});
