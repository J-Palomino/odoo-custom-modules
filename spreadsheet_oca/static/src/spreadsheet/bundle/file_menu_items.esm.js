/** @odoo-module **/
// OCA additions to the core "file" topbar menu.
// The parent "file" menu entry is registered by Odoo 19 core —
// we only add child items here (addChild), never re-add the parent.

import * as spreadsheet from "@odoo/o-spreadsheet";
import {_t} from "@web/core/l10n/translation";

const {topbarMenuRegistry} = spreadsheet.registries;

topbarMenuRegistry.addChild("save", ["file"], {
    name: _t("Save"),
    sequence: 10,
    execute: (env) => env.saveSpreadsheet(),
    icon: "o-spreadsheet-Icon.DOWNLOAD",
});
topbarMenuRegistry.addChild("download", ["file"], {
    name: _t("Download XLSX"),
    sequence: 20,
    execute: (env) => env.downloadAsXLXS(),
    icon: "o-spreadsheet-Icon.EXPORT_XLSX",
});
