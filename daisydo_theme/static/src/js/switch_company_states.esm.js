/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import { session } from "@web/session";
import { _t } from "@web/core/l10n/translation";
import { SwitchCompanyMenu } from "@web/webclient/switch_company_menu/switch_company_menu";

/**
 * Region accent colours for the company switcher.
 *
 * Named states get a fixed brand-ish colour; any state not listed falls back to
 * a deterministic hue derived from its name, so a new region still gets a stable
 * distinct accent with no code change. Consumed by the systray trigger dot and
 * the state subheaders (switch_company_menu.xml); styled in theme.scss.
 */
const REGION_COLORS = {
    Arizona: "#d9822b",
    Michigan: "#2b6cd9",
    Florida: "#d92b6c",
    Illinois: "#6c2bd9",
    Nevada: "#1f9d6b",
    Missouri: "#b59a14",
};

/**
 * Group the multi-company switcher list by US state.
 *
 * Odoo's base `computeVisibleCompanies()` returns a flat, hierarchy-ordered
 * list of `{ company, level }` entries. We keep those entries (so selectAll /
 * search / toggle behaviour is untouched) but annotate each with `stateName`
 * and reorder them into state buckets. The template
 * (daisydo_theme.SwitchCompanyMenuItems) renders a clickable subheader whenever
 * the state changes between two consecutive rows; clicking it stages every
 * store in that state (the existing Confirm button then applies the switch).
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
        const sorted = entries
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

        // The org root ("Mint Cannabis") is rendered as a master row above the
        // groups, so drop its own entry here to avoid showing it twice.
        const rootId = session.company_root_id;
        return rootId ? sorted.filter((entry) => entry.company.id !== rootId) : sorted;
    },

    /** Name of the org-root master row, or "" when grouping is inactive. */
    get rootName() {
        return session.company_root_name || "";
    },

    /** Every company the master row controls: all grouped rows plus the root. */
    masterCompanyIds() {
        const ids = this.visibleCompanies.map((entry) => entry.company.id);
        const rootId = session.company_root_id;
        if (rootId && !ids.includes(rootId)) {
            ids.push(rootId);
        }
        return ids;
    },

    /** Toggle the whole org: select all companies if not all selected, else clear. */
    selectMaster() {
        this.companySelector.selectAll(this.masterCompanyIds());
    },

    /** Tri-state checkbox icon for the master row. */
    masterIcon() {
        const ids = this.masterCompanyIds();
        const selected = ids.filter((id) => this.companySelector.isCompanySelected(id));
        if (ids.length && selected.length === ids.length) {
            return "fa-check-square text-primary";
        }
        if (selected.length) {
            return "fa-minus-square-o";
        }
        return "fa-square-o";
    },

    /** Company ids currently shown under a given state header. */
    stateCompanyIds(stateName) {
        return this.visibleCompanies
            .filter((entry) => entry.stateName === stateName)
            .map((entry) => entry.company.id);
    },

    /** Toggle the whole state group: select all if none/some, else deselect all. */
    selectState(stateName) {
        this.companySelector.selectAll(this.stateCompanyIds(stateName));
    },

    /** Tri-state checkbox icon for a state header (mirrors the global selectAllIcon). */
    stateSelectIcon(stateName) {
        const ids = this.stateCompanyIds(stateName);
        const selected = ids.filter((id) => this.companySelector.isCompanySelected(id));
        if (ids.length && selected.length === ids.length) {
            return "fa-check-square text-primary";
        }
        if (selected.length) {
            return "fa-minus-square-o";
        }
        return "fa-square-o";
    },

    // ---- Location-filter UX affordances (labelled trigger, preview, colour) ----

    /**
     * Accent colour for a state: fixed brand colour, else a deterministic hue
     * derived from the name so unlisted regions are still stable and distinct.
     */
    stateAccentColor(stateName) {
        if (!stateName) {
            return "transparent";
        }
        if (REGION_COLORS[stateName]) {
            return REGION_COLORS[stateName];
        }
        let hash = 0;
        for (let i = 0; i < stateName.length; i++) {
            hash = (hash * 31 + stateName.charCodeAt(i)) >>> 0;
        }
        return `hsl(${hash % 360}, 55%, 42%)`;
    },

    /** Distinct region names among the currently *active* (applied) stores. */
    activeRegionNames() {
        const states = session.company_states || {};
        const rootId = session.company_root_id;
        const names = (this.user.activeCompanies || [])
            .filter((company) => company.id !== rootId)
            .map((company) => states[company.id])
            .filter(Boolean);
        return [...new Set(names)];
    },

    /** Colour dot for the systray trigger when exactly one region is active. */
    get activeRegionColor() {
        const names = this.activeRegionNames();
        return names.length === 1 ? this.stateAccentColor(names[0]) : "";
    },

    /**
     * Location-first label for the systray trigger, e.g. "Arizona · 3 stores"
     * or "2 regions · 12 stores". Falls back to the active company name when no
     * state-bearing store is active (corporate-only context).
     */
    get locationLabel() {
        const rootId = session.company_root_id;
        const stores = (this.user.activeCompanies || []).filter(
            (company) => company.id !== rootId
        );
        const regions = this.activeRegionNames();
        const noun = stores.length === 1 ? _t("store") : _t("stores");
        let region;
        if (regions.length === 1) {
            region = regions[0];
        } else if (regions.length === 0) {
            return this.user.activeCompany.name;
        } else {
            region = `${regions.length} ${_t("regions")}`;
        }
        return `${region} · ${stores.length} ${noun}`;
    },

    /** Tooltip that frames the switcher as the location filter it actually is. */
    get locationTitle() {
        return `${_t("Filter records by location — currently showing")}: ${this.locationLabel}`;
    },

    /**
     * Plain-language preview of the *staged* selection, shown above Confirm so
     * users see what the switch will do before applying it. Built from
     * `allowedCompaniesWithAncestors` (not the filtered visible list) so the
     * preview is accurate even while the search box is filtering the rows.
     */
    get stagedPreviewText() {
        const states = session.company_states || {};
        const rootId = session.company_root_id;
        const OTHER = _t("Other");
        const byId = {};
        for (const company of this.user.allowedCompaniesWithAncestors || []) {
            byId[company.id] = company;
        }
        const stores = (this.companySelector.selectedCompaniesIds || [])
            .map((id) => byId[id])
            .filter(Boolean)
            .filter((company) => company.id !== rootId);
        if (!stores.length) {
            return _t("No locations selected — records will be hidden until you pick at least one.");
        }
        const byState = {};
        for (const company of stores) {
            const stateName = states[company.id] || OTHER;
            if (!byState[stateName]) {
                byState[stateName] = [];
            }
            byState[stateName].push(company.name);
        }
        const parts = Object.keys(byState)
            .sort()
            .map((stateName) => `${stateName} (${byState[stateName].join(", ")})`);
        const noun = stores.length === 1 ? _t("store") : _t("stores");
        return `${_t("About to show records for")}: ${parts.join("; ")} — ${stores.length} ${noun}`;
    },
});
