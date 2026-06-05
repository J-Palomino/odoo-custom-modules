/** @odoo-module **/

import { registry } from "@web/core/registry";
import { KanbanController } from "@web/views/kanban/kanban_controller";
import { useService } from "@web/core/utils/hooks";
import { onWillStart, onWillUnmount } from "@odoo/owl";

/**
 * Resolve the active company id without depending on a single registry token.
 *
 * In some Odoo 19 builds the `company` service throws on `useService("company")`
 * (registered under a different token, removed, or not yet hydrated when the
 * kanban setup runs). Try the most-likely-to-exist sources in order:
 *   1. `company` service (preferred — reactive)
 *   2. `user` service activeCompany (stable across Odoo 16-19)
 *   3. `odoo.session_info.user_companies.current_company` (set at boot,
 *      no service required — value is either `id` or `[id, name]`)
 *
 * Returns null if all three fail; caller skips the bus subscription so the
 * kanban renders without live updates instead of crashing.
 */
function resolveCompanyId(env) {
    const services = (env && env.services) || {};

    if (services.company && services.company.currentCompany) {
        return services.company.currentCompany.id;
    }
    if (services.user && services.user.activeCompany) {
        return services.user.activeCompany.id;
    }
    if (typeof odoo !== "undefined" && odoo.session_info && odoo.session_info.user_companies) {
        const current = odoo.session_info.user_companies.current_company;
        if (Array.isArray(current)) return current[0];
        if (typeof current === "number") return current;
        if (current && typeof current === "object") return current.id;
    }
    return null;
}

/**
 * Live-updating Kanban controller for mint.pos.order.
 *
 * Subscribes to bus.bus channel `mint_pos_{company_id}` and auto-reloads
 * the kanban when orders are created or move between lanes.
 * Plays a notification sound on new order arrival.
 */
class MintPosKanbanController extends KanbanController {
    setup() {
        super.setup();
        this.busService = useService("bus_service");
        this.notification = useService("notification");

        this._channel = null;

        onWillStart(() => {
            const companyId = resolveCompanyId(this.env);
            if (!companyId) {
                console.warn(
                    "[mint_pos_kanban_live] could not resolve current company id — " +
                    "skipping bus subscription; kanban will need manual refresh."
                );
                return;
            }
            this._channel = `mint_pos_${companyId}`;
            this.busService.subscribe(this._channel, (payload) => {
                this._onBusMessage(payload);
            });
        });

        onWillUnmount(() => {
            if (this._channel) {
                this.busService.unsubscribe(this._channel);
            }
        });
    }

    _onBusMessage(payload) {
        if (!payload) return;

        const { name, event } = payload;

        // Reload the kanban view to reflect the change (new order or lane move)
        this.model.load();

        // Chime + toast only for brand-new transactions arriving in this store,
        // not for every lane move. The model tags create() pushes with
        // event === "create"; lane moves send event === "update".
        if (event === "create") {
            this.notification.add(
                `New order: ${name || "Unknown"}`,
                { type: "info", sticky: false }
            );
            this._playNotificationSound();
        }
    }

    _playNotificationSound() {
        try {
            // Use Web Audio API for a brief chime
            const ctx = new (window.AudioContext || window.webkitAudioContext)();
            const oscillator = ctx.createOscillator();
            const gain = ctx.createGain();
            oscillator.connect(gain);
            gain.connect(ctx.destination);
            oscillator.frequency.value = 880; // A5 note
            oscillator.type = "sine";
            gain.gain.value = 0.15;
            oscillator.start();
            gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.3);
            oscillator.stop(ctx.currentTime + 0.3);
        } catch {
            // Audio not available — silently skip
        }
    }
}

// Register as a kanban view variant for mint.pos.order
registry.category("views").add("mint_pos_kanban_live", {
    ...registry.category("views").get("kanban"),
    Controller: MintPosKanbanController,
});
