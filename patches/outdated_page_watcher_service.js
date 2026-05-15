/** @odoo-module **/
// PATCHED: Neutered outdated page watcher (odoo/odoo#188562 timezone bug).
// Keeps exports intact so other modules importing from this file don't break.
// The service start() is a no-op — no watcher runs, no false notifications.

import { registry } from "@web/core/registry";

export class OutdatedPageWatcherService {
    constructor() {}
    showOutdatedPageNotification() {}
}

export const outdatedPageWatcherService = {
    dependencies: ["bus_service", "multi_tab", "notification"],
    start() {},
};

registry.category("services").add("bus.outdated_page_watcher", outdatedPageWatcherService);
