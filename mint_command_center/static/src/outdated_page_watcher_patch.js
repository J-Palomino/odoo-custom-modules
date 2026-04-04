/** @odoo-module **/
import { OutdatedPageWatcherService } from "@bus/outdated_page_watcher_service";
import { patch } from "@web/core/utils/patch";

// Suppress the "page is out of date" notification entirely. The default
// behavior shows a persistent sticky warning every time the websocket
// reconnects (e.g. after autovacuum or brief worker saturation). The
// previous patch did window.location.reload() which interrupted saves
// and corrupted form data. Now we simply ignore it — users can manually
// refresh if needed.
patch(OutdatedPageWatcherService.prototype, {
    showOutdatedPageNotification() {
        // No-op: suppress the notification without reloading.
        // A silent reload mid-save causes data loss and form corruption.
    },
});
