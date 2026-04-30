/** @odoo-module **/

import { registry } from "@web/core/registry";
import { KanbanController } from "@web/views/kanban/kanban_controller";
import { useService } from "@web/core/utils/hooks";
import { onWillStart, onWillUnmount } from "@odoo/owl";

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

        // Look up the company service via the registry rather than useService():
        // useService throws synchronously if the token isn't registered, which
        // takes the whole kanban down. The bus subscription is purely a
        // live-update nice-to-have — losing it should degrade to a manual-refresh
        // kanban, not crash the page. Tolerates upstream rename of the service
        // token between Odoo versions.
        this.companyService =
            this.env.services.company ||
            this.env.services.companyService ||
            null;

        this._channel = null;

        onWillStart(() => {
            if (!this.companyService) {
                console.warn(
                    "[mint_pos_kanban_live] company service unavailable — " +
                    "skipping bus subscription; kanban will need manual refresh."
                );
                return;
            }
            const companyId = this.companyService.currentCompany.id;
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

        const { id, name, state } = payload;

        // Reload the kanban view to reflect the change
        this.model.load();

        // Show notification for new orders
        if (state === "online_orders" || state === "placed" || state === "lobby") {
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
