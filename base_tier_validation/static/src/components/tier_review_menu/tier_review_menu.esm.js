import {Component, useState} from "@odoo/owl";
import {Dropdown} from "@web/core/dropdown/dropdown";
import {registry} from "@web/core/registry";
import {useDropdownState} from "@web/core/dropdown/dropdown_hooks";
import {useService} from "@web/core/utils/hooks";

// Why: Odoo 19's asset bundler does not reliably handle top-level await
// in `web.assets_backend` modules — a previous `await import(...)` at
// module scope caused the entire backend SPA to white-page during init
// (the systray failed to load before any /web/dataset/call_kw could fire).
// We use a static fallback shape; the discuss-systray styling is purely
// cosmetic and the menu functions identically without it.
const _discussSystrayFallback = {menuClass: "", contentClass: ""};

export class TierReviewMenu extends Component {
    static components = {Dropdown};
    static props = [];
    static template = "base_tier_validation.TierReviewMenu";

    setup() {
        super.setup();
        this.discussSystray = _discussSystrayFallback;
        this.orm = useService("orm");
        this.store = useState(useService("mail.store"));
        this.action = useService("action");
        this.dropdown = useDropdownState();
        this.fetchSystrayReviewer();
    }

    async fetchSystrayReviewer() {
        const groups = await this.orm.call("res.users", "review_user_count");
        let total = 0;
        for (const group of groups) {
            total += group.pending_count || 0;
        }
        this.store.tierReviewCounter = total;
        this.store.tierReviewGroups = groups;
    }

    availableViews() {
        return [
            [false, "kanban"],
            [false, "list"],
            [false, "form"],
            [false, "activity"],
        ];
    }

    openReviewGroup(group) {
        this.dropdown.close();
        const context = {};
        const domain = [["can_review", "=", true]];
        if (group.active_field) {
            domain.push(["active", "in", [true, false]]);
        }
        const views = this.availableViews();

        this.action.doAction(
            {
                context,
                domain,
                name: group.name,
                res_model: group.model,
                search_view_id: [false],
                type: "ir.actions.act_window",
                views,
            },
            {
                clearBreadcrumbs: true,
            }
        );
    }
}

export const systrayItem = {
    Component: TierReviewMenu,
};

registry
    .category("systray")
    .add("base_tier_validation.ReviewerMenu", systrayItem, {sequence: 99});
