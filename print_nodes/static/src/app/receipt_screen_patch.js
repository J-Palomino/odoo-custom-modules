/** @odoo-module **/

import { ReceiptScreen } from "@point_of_sale/app/screens/receipt_screen/receipt_screen";
import { patch } from "@web/core/utils/patch";
import { useService } from "@web/core/utils/hooks";
import { onMounted } from "@odoo/owl";

/**
 * Drive a local Zebra ZD410 from the POS receipt screen.
 *
 *  - On mount after payment, auto-print (label + receipt per pos.config),
 *    when mint_zebra_autoprint is on. No user gesture here, so a not-yet-
 *    paired USB printer falls back to PrintNode (auto mode) or prompts the
 *    cashier to tap Print once.
 *  - The native "Print Receipt" button is augmented to also print to the
 *    Zebra; that click is a user gesture, so it can pair a USB printer.
 */
patch(ReceiptScreen.prototype, {
    setup() {
        super.setup(...arguments);
        this.mintZebra = useService("mint_zebra");
        onMounted(() => this._mintZebraAutoprint());
    },

    _mintZebraConfig() {
        return (this.pos && this.pos.config) || {};
    },

    _mintZebraOrderRef() {
        const order = (this.pos && this.pos.get_order && this.pos.get_order()) || null;
        if (!order) {
            return null;
        }
        return order.uuid || order.pos_reference || order.name || null;
    },

    async _mintZebraPrint(allowRequest) {
        const config = this._mintZebraConfig();
        if (!config.mint_zebra_enabled) {
            return;
        }
        const ref = this._mintZebraOrderRef();
        if (!ref) {
            return;
        }
        await this.mintZebra.print({
            ref,
            transport: config.mint_zebra_transport || "auto",
            printLabel: config.mint_zebra_print_label,
            printReceipt: config.mint_zebra_print_receipt,
            // Without these the local-agent transport calls 127.0.0.1 with no
            // token, so any agent started with MINT_AGENT_TOKEN set — as
            // AGENT_INSTALL.md instructs — answers 401.
            agentUrl: config.mint_zebra_agent_url,
            agentToken: config.mint_zebra_agent_token,
            allowRequest,
        });
    },

    async _mintZebraAutoprint() {
        const config = this._mintZebraConfig();
        if (!config.mint_zebra_enabled || !config.mint_zebra_autoprint) {
            return;
        }
        await this._mintZebraPrint(false);
    },

    async printReceipt() {
        const res = await super.printReceipt(...arguments);
        // Button click = user gesture, so WebUSB pairing is allowed here.
        await this._mintZebraPrint(true);
        return res;
    },
});
