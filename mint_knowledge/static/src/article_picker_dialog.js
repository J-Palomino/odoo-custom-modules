/** @odoo-module **/

import { Component, useState, onWillStart } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";
import { useService } from "@web/core/utils/hooks";

/**
 * Modal that lets an author search Knowledge Base articles and pick one to
 * link. Calls props.onSelect(page) with {id, name, slug, category_id} then
 * closes.
 */
export class ArticlePickerDialog extends Component {
    static template = "mint_knowledge.ArticlePickerDialog";
    static components = { Dialog };
    static props = { close: Function, onSelect: Function };

    setup() {
        this.orm = useService("orm");
        this.state = useState({ query: "", pages: [] });
        onWillStart(() => this.search(""));
    }

    async search(query) {
        const domain = query
            ? ["|", ["name", "ilike", query], ["content", "ilike", query]]
            : [];
        this.state.pages = await this.orm.searchRead(
            "mint.knowledge.page",
            domain,
            ["id", "name", "slug", "category_id"],
            { limit: 50, order: "name" }
        );
    }

    onInput(ev) {
        this.state.query = ev.target.value;
        this.search(this.state.query);
    }

    onSelect(page) {
        this.props.onSelect(page);
        this.props.close();
    }
}
