/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";
import { Plugin } from "@html_editor/plugin";
import { withSequence } from "@html_editor/utils/resource";
import { HtmlField } from "@html_editor/fields/html_field";
import { ArticlePickerDialog } from "./article_picker_dialog";

/**
 * Adds a "Link to article" powerbox command to the Knowledge Base page editor.
 * Type `/`, filter by "article" / "link" / "knowledge" / "kb", pick the command
 * to open a searchable article picker, and insert a hyperlink to the chosen
 * article at the cursor.
 *
 * Verified against Odoo 19 source:
 *   - html_editor/static/src/plugin.js (this.services from EditorContext)
 *   - html_editor/static/src/main/link/link_plugin.js (shared insertLink(url,label))
 *   - html_editor/static/src/fields/html_field.js (getConfig() builds config.Plugins)
 *   - html_editor/static/src/main/powerbox/powerbox_plugin.js (resources idiom)
 */
export class MintArticleLinkPlugin extends Plugin {
    static id = "mintArticleLink";
    static dependencies = ["link", "history"];

    resources = {
        user_commands: [
            {
                id: "mintInsertArticleLink",
                title: _t("Link to article"),
                description: _t("Insert a link to a knowledge base article"),
                icon: "fa-book",
                run: this.openArticlePicker.bind(this),
            },
        ],
        powerbox_categories: withSequence(55, {
            id: "mintKnowledge",
            name: _t("Knowledge"),
        }),
        powerbox_items: [
            {
                categoryId: "mintKnowledge",
                commandId: "mintInsertArticleLink",
                keywords: [_t("article"), _t("link"), _t("knowledge"), _t("kb")],
            },
        ],
    };

    openArticlePicker() {
        this.services.dialog.add(ArticlePickerDialog, {
            onSelect: (page) => {
                const url = page.slug
                    ? `/knowledge/p/${page.slug}`
                    : `/knowledge/page/${page.id}`;
                this.dependencies.link.insertLink(url, page.name);
                this.dependencies.history.addStep();
            },
        });
    }
}

// Inject the plugin only into the Knowledge page "content" editor — every other
// html field on the page is left untouched.
patch(HtmlField.prototype, {
    getConfig() {
        const config = super.getConfig();
        if (
            this.props.record?.resModel === "mint.knowledge.page" &&
            this.props.name === "content"
        ) {
            config.Plugins = [...(config.Plugins || []), MintArticleLinkPlugin];
        }
        return config;
    },
});
