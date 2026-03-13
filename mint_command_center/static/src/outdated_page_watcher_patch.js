/** @odoo-module **/
import { OutdatedPageWatcherService } from "@bus/outdated_page_watcher_service";
import { patch } from "@web/core/utils/patch";

// Patch the outdated page watcher to silently refresh instead of showing
// a persistent sticky notification. The default behavior shows "The page
// is out of date" every time the websocket reconnects after the autovacuum
// cron purges old bus_bus records — which is normal operation, not an error.
patch(OutdatedPageWatcherService.prototype, {
    showOutdatedPageNotification() {
        // Instead of a sticky warning that blocks the user, silently reload.
        // This avoids the constant "page is out of date" prompts while still
        // keeping the page current.
        window.location.reload();
    },
});
