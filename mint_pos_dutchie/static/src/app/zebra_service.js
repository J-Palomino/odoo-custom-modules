/** @odoo-module **/

import { registry } from "@web/core/registry";
import { _t } from "@web/core/l10n/translation";
import { ZebraWebUsb } from "./zebra_webusb";
import { ZebraLocalAgent } from "./zebra_local_agent";

/**
 * Orchestrates printing a POS order to a Zebra ZD410.
 *
 *  - WebUSB:     direct from the browser (client-side, Mac/ChromeOS-friendly).
 *  - local_agent: POST ZPL to a localhost print agent on the register (uses
 *                 the OS driver — no Zadig; best for Windows). Self-hosted.
 *  - PrintNode:  server-side proxy (pos.order.mint_printnode_print) so the
 *                API key never reaches the cashier's browser; cloud SaaS.
 *  - auto:       WebUSB -> local_agent -> PrintNode.
 *
 * The ZPL itself is always generated server-side from reliable ORM fields
 * (pos.order.get_mint_zebra_zpl) so output does not depend on POS JS getters.
 */
export const mintZebraService = {
    dependencies: ["orm", "notification"],
    start(env, { orm, notification }) {
        let cachedDevice = null;

        async function ensureUsbDevice(allowRequest) {
            if (cachedDevice) {
                return cachedDevice;
            }
            cachedDevice = await ZebraWebUsb.getPairedZebra();
            if (!cachedDevice && allowRequest && ZebraWebUsb.isSupported()) {
                cachedDevice = await ZebraWebUsb.requestZebra();
            }
            return cachedDevice;
        }

        async function getZpl(ref, which) {
            const zpl = await orm.call("pos.order", "get_mint_zebra_zpl", [ref, which]);
            if (!zpl || zpl.error) {
                throw new Error((zpl && zpl.error) || "no_zpl");
            }
            return zpl;
        }

        async function printViaWebUsb(ref, which, allowRequest) {
            if (!ZebraWebUsb.isSupported()) {
                return { ok: false, error: "webusb_unsupported" };
            }
            const device = await ensureUsbDevice(allowRequest);
            if (!device) {
                return { ok: false, error: "no_usb_device" };
            }
            const zpl = await getZpl(ref, which);
            if (zpl.label) {
                await ZebraWebUsb.send(device, zpl.label);
            }
            if (zpl.receipt) {
                await ZebraWebUsb.send(device, zpl.receipt);
            }
            return { ok: true, transport: "webusb" };
        }

        async function printViaLocalAgent(ref, which, agentUrl, agentToken) {
            if (!(await ZebraLocalAgent.isAvailable(agentUrl))) {
                return { ok: false, error: "no_local_agent" };
            }
            const zpl = await getZpl(ref, which);
            if (zpl.label) {
                await ZebraLocalAgent.send(agentUrl, zpl.label, { token: agentToken });
            }
            if (zpl.receipt) {
                await ZebraLocalAgent.send(agentUrl, zpl.receipt, { token: agentToken });
            }
            return { ok: true, transport: "local_agent" };
        }

        async function printViaPrintNode(ref, which) {
            const res = await orm.call("pos.order", "mint_printnode_print", [ref, which]);
            return { ...res, transport: "printnode" };
        }

        /**
         * @param {Object} opts
         * @param {string} opts.ref          order uuid / pos_reference
         * @param {string} opts.transport    "auto" | "webusb" | "printnode"
         * @param {boolean} opts.printLabel
         * @param {boolean} opts.printReceipt
         * @param {boolean} opts.allowRequest  true when within a user gesture
         * @param {boolean} opts.silent        suppress success/info toasts
         */
        async function print(opts) {
            const {
                ref,
                transport = "auto",
                printLabel = true,
                printReceipt = true,
                allowRequest = false,
                silent = false,
            } = opts;

            const which =
                printLabel && printReceipt
                    ? "both"
                    : printLabel
                    ? "label"
                    : printReceipt
                    ? "receipt"
                    : null;
            if (!which) {
                return { ok: false, error: "nothing_selected" };
            }
            if (!ref) {
                return { ok: false, error: "no_order_ref" };
            }

            const { agentUrl, agentToken } = opts;
            const tryWebUsb = () =>
                printViaWebUsb(ref, which, allowRequest).catch((e) => ({ ok: false, error: e.message }));
            const tryAgent = () =>
                printViaLocalAgent(ref, which, agentUrl, agentToken).catch((e) => ({ ok: false, error: e.message }));

            let result;
            if (transport === "printnode") {
                result = await printViaPrintNode(ref, which);
            } else if (transport === "webusb") {
                result = await tryWebUsb();
            } else if (transport === "local_agent") {
                result = await tryAgent();
            } else {
                // auto: WebUSB -> local agent -> PrintNode
                result = await tryWebUsb();
                if (!result.ok && result.error !== "no_zpl") {
                    result = await tryAgent();
                }
                if (!result.ok && result.error !== "no_zpl") {
                    result = await printViaPrintNode(ref, which);
                }
            }

            if (result.ok) {
                if (!silent) {
                    notification.add(_t("Printed to Zebra (%s)", result.transport), {
                        type: "success",
                    });
                }
            } else if (result.error === "no_usb_device" && !allowRequest) {
                // No paired USB printer yet and we have no gesture to prompt.
                notification.add(
                    _t("Tap “Print Receipt” once to connect the Zebra printer."),
                    { type: "info" }
                );
            } else {
                notification.add(
                    _t("Zebra print failed: %s", result.error || "unknown error"),
                    { type: "danger" }
                );
            }
            return result;
        }

        return { print, ensureUsbDevice, isUsbSupported: ZebraWebUsb.isSupported };
    },
};

registry.category("services").add("mint_zebra", mintZebraService);
