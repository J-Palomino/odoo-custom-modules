/** @odoo-module **/
//
// Aggressively unregister any Odoo service worker and clear its caches on
// backend page load. The Odoo backend does not use SW for offline support,
// so eviction is safe — and a stale SW from a previous deploy can wedge the
// bus on JS bundles that call removed endpoints (e.g. the OUTDATED_VERSION
// loop and the missing /bus/get_autojoin_bus_channels route we hit in 2026-04).
//
// Frontend Astro PWA SW (different scope, different script URL, different
// cache names) is left alone.
//

(async function bustOdooServiceWorker() {
    if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) {
        return;
    }

    let removedSomething = false;

    try {
        const regs = await navigator.serviceWorker.getRegistrations();
        for (const reg of regs) {
            const scriptURL =
                reg.active?.scriptURL ||
                reg.installing?.scriptURL ||
                reg.waiting?.scriptURL ||
                "";
            // Match Odoo's service worker by path. Odoo registers it as
            // /service-worker.js (with optional /web/ prefix), so the URL
            // ends with /service-worker.js. Anything else is third-party.
            const isOdooSW =
                scriptURL.endsWith("/service-worker.js") ||
                scriptURL.includes("/service-worker.js?");
            if (isOdooSW) {
                const ok = await reg.unregister();
                if (ok) {
                    removedSomething = true;
                    console.warn(
                        "[mint_sw_buster] unregistered stale Odoo SW:",
                        scriptURL,
                    );
                }
            }
        }

        if (typeof caches !== "undefined") {
            const names = await caches.keys();
            for (const name of names) {
                // Odoo's default cache is `odoo-sw-cache`. Frontend caches
                // are `static-mint-v1`, `api-mint-v1`, `mint-v1` — they
                // don't contain "odoo".
                if (name.includes("odoo")) {
                    const ok = await caches.delete(name);
                    if (ok) {
                        removedSomething = true;
                        console.warn(
                            "[mint_sw_buster] cleared stale Odoo cache:",
                            name,
                        );
                    }
                }
            }
        }
    } catch (e) {
        console.warn("[mint_sw_buster] error during eviction:", e);
        return;
    }

    // Force ONE clean reload after eviction so the page picks up fresh assets
    // (the dead SW may have served stale JS into the current page). The
    // sessionStorage guard guarantees we never loop.
    const GUARD = "mint_sw_buster_reloaded";
    if (removedSomething && !sessionStorage.getItem(GUARD)) {
        sessionStorage.setItem(GUARD, "1");
        console.warn("[mint_sw_buster] reloading once to pick up fresh assets");
        location.reload();
    } else if (!removedSomething) {
        // Clean state — clear the guard so a future eviction can reload again.
        sessionStorage.removeItem(GUARD);
    }
})();
