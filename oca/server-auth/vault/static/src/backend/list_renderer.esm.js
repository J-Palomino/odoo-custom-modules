import {ListRenderer} from "@web/views/list/list_renderer";
import {patch} from "@web/core/utils/patch";

patch(ListRenderer.prototype, {
    getCellTitle(column) {
        const _super = super.getCellTitle.bind(this);
        const widget = column.widget || (column.rawAttrs && column.rawAttrs.widget) || "";
        if (widget !== "vault_field") return _super(...arguments);
    },
});
