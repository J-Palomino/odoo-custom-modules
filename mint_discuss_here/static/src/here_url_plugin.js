/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";
import { Plugin } from "@html_editor/plugin";
import { withSequence } from "@html_editor/utils/resource";
import { Composer } from "@mail/core/common/composer";

/**
 * Adds a "/here" powerbox command to the Discuss / chatter composer that
 * inserts the current page URL inline at the cursor.
 *
 * This rides the same `/` command menu as the built-in html_editor powerbox
 * commands: type `/`, filter by "here" (or url / link / location), and pick
 * "Current URL". The powerbox removes the typed `/here` query before running,
 * so the result is just the URL pasted at the cursor — a natural paste feel.
 *
 * Verified against Odoo 19 source:
 *   - addons/html_editor/static/src/main/powerbox/powerbox_plugin.js
 *     (user_commands / powerbox_categories / powerbox_items + keywords)
 *   - addons/html_editor/static/src/main/separator_plugin.js (command idiom)
 *   - addons/html_editor/static/src/core/dom_plugin.js (id "dom", insert(str))
 *   - addons/mail/static/src/core/common/composer.js (wysiwygConfig.Plugins)
 */
export class MintHereUrlPlugin extends Plugin {
    static id = "mintHereUrl";
    static dependencies = ["dom", "history"];

    resources = {
        user_commands: [
            {
                id: "mintInsertCurrentUrl",
                title: _t("Current URL"),
                description: _t("Insert the current page URL"),
                icon: "fa-link",
                run: this.insertCurrentUrl.bind(this),
            },
        ],
        // Slot just below the "Widget" category (sequence 60).
        powerbox_categories: withSequence(55, { id: "mint", name: _t("Mint") }),
        powerbox_items: [
            {
                categoryId: "mint",
                commandId: "mintInsertCurrentUrl",
                keywords: [_t("here"), _t("url"), _t("link"), _t("location")],
            },
        ],
    };

    insertCurrentUrl() {
        // Read from the editor's own document/window so it stays correct even
        // if the composer is ever hosted in an iframe; fall back to top window.
        const url =
            this.document?.defaultView?.location?.href || window.location.href;
        this.dependencies.dom.insert(url);
        this.dependencies.history.addStep();
    }
}

// Append the plugin to the composer's Wysiwyg config only. Spreading into a
// fresh array avoids mutating the shared MAIL_PLUGINS / MAIL_SMALL_UI_PLUGINS
// const arrays, so other html_editor instances on the page are unaffected.
patch(Composer.prototype, {
    get wysiwygConfig() {
        const config = super.wysiwygConfig;
        if (config.Plugins && !config.Plugins.includes(MintHereUrlPlugin)) {
            config.Plugins = [...config.Plugins, MintHereUrlPlugin];
        }
        return config;
    },
});
