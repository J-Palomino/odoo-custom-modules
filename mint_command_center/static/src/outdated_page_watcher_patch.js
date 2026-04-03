/** @odoo-module **/
import { OutdatedPageWatcherService } from "@bus/outdated_page_watcher_service";
import { patch } from "@web/core/utils/patch";
import { session } from "@web/session";

// Suppress the "page is out of date" notification for everyone except Juan
// (uid 2). The default behavior shows a persistent sticky warning every time
// the websocket reconnects after deploy. Juan keeps it so he can monitor
// deploy health.
patch(OutdatedPageWatcherService.prototype, {
    showOutdatedPageNotification() {
        if (session.uid === 2) {
            // Let the original notification show for Juan
            return super.showOutdatedPageNotification(...arguments);
        }
        // No-op for everyone else
    },
});
