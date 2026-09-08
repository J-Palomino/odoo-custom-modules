/** @odoo-module **/

import { ProductScreen } from "@point_of_sale/app/screens/product_screen/product_screen";
import { useBarcodeReader } from "@point_of_sale/app/hooks/barcode_reader_hook";
import { patch } from "@web/core/utils/patch";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { ZebraLocalAgent } from "./zebra_local_agent";

/**
 * Print a product/shelf label whenever a product barcode is scanned at the
 * register, when `mint_print_label_on_scan` is enabled on the pos.config.
 *
 * This registers a NON-exclusive `product` handler via useBarcodeReader, so
 * Odoo's own product handler (add-to-order) still runs — the barcode reader
 * service dispatches a scan to EVERY registered handler for that code type, so
 * the two coexist: Odoo adds the item, we print its label.
 *
 * The label goes through the register's local print agent (the Windows print
 * node), the same transport the receipt screen uses. WebUSB needs a user
 * gesture we don't have inside a scan callback, and PrintNode is the cloud
 * fallback; for scan-driven label printing the local agent is the right path.
 */
patch(ProductScreen.prototype, {
    setup() {
        super.setup(...arguments);
        this.orm = useService("orm");
        this.notification = useService("notification");
        const config = (this.pos && this.pos.config) || {};
        if (config.mint_print_label_on_scan) {
            useBarcodeReader({
                product: (code) => this._mintPrintScannedLabel(code),
            });
        }
    },

    /**
     * @param {Object} code parsed barcode from the reader (has base_code/code)
     */
    async _mintPrintScannedLabel(code) {
        const barcode = (code && (code.base_code || code.code)) || "";
        if (!barcode) {
            return;
        }
        const config = (this.pos && this.pos.config) || {};
        try {
            // Build the ZPL server-side from reliable ORM fields (never depend on
            // POS JS getters), mirroring how the order label/receipt is generated.
            const zpl = await this.orm.call(
                "pos.config",
                "mint_scan_product_label_zpl",
                [config.id, barcode]
            );
            if (!zpl) {
                return; // no product matched — stay quiet; Odoo shows its own note
            }
            if (!(await ZebraLocalAgent.isAvailable())) {
                this.notification.add(
                    _t("Scanned label not printed: the local print agent is not running on this register."),
                    { type: "warning" }
                );
                return;
            }
            await ZebraLocalAgent.send(undefined, zpl, {});
        } catch (e) {
            this.notification.add(_t("Label print on scan failed: %s", (e && e.message) || e), {
                type: "warning",
            });
        }
    },
});
