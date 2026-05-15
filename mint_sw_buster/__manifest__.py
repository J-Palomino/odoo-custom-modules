{
    "name": "Mint Service Worker Buster",
    "version": "19.0.1.0.0",
    "summary": "Unregister stale Odoo service workers on backend page load",
    "description": """
Aggressively unregisters Odoo's own service worker and clears its caches on
every backend page load. The Odoo backend doesn't use a service worker for
offline support, but stale SWs from previous deploys can wedge clients on
JS bundles that call removed endpoints — most notably the bus SharedWorker,
which leaves users with a permanently DISCONNECTED bus and no notifications.

The eviction is scoped to Odoo SWs only (script URL match + cache name match);
any third-party SW (e.g. the public website's PWA cache) is left alone.

A sessionStorage guard ensures we reload at most once per session after a
successful eviction, so the script can never trigger a reload loop.
    """,
    "category": "Tools",
    "author": "Mint Cannabis",
    "website": "https://letsgomint.us",
    "license": "LGPL-3",
    "depends": ["web"],
    "assets": {
        "web.assets_backend": [
            "mint_sw_buster/static/src/sw_buster.js",
        ],
    },
    "installable": True,
    "application": False,
    "auto_install": True,
}
