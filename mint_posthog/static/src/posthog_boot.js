/** @odoo-module **/
/**
 * PostHog Analytics for Odoo Backend
 * ===================================
 * Captures JS errors, RPC failures, and session recordings
 * so we can see exactly what errors admins encounter.
 *
 * Dedicated "LetsGoMint" PostHog project (544449) — separate from the
 * MintDeals2.0 storefront project so Odoo backend data stays isolated.
 * Filter by person property `odoo_user` or `app: "odoo-backend"`.
 */

import { registry } from "@web/core/registry";
import { session } from "@web/session";
import { browser } from "@web/core/browser/browser";

const POSTHOG_KEY = "phc_pn6qyCiqURG5TQodSwspowqCYQiKGz92tD3N2GeCjRBT";
const POSTHOG_HOST = "https://us.i.posthog.com";

let _posthogReady = false;

/**
 * Load and initialize PostHog.
 */
function bootPostHog() {
    if (_posthogReady || typeof window === "undefined") return;
    _posthogReady = true;

    // --- PostHog snippet (minified loader) ---
    !function(t,e){var o,n,p,r;e.__SV||(window.posthog=e,e._i=[],e.init=function(i,s,a){function g(t,e){var o=e.split(".");2==o.length&&(t=t[o[0]],e=o[1]),t[e]=function(){t.push([e].concat(Array.prototype.slice.call(arguments,0)))}}(p=t.createElement("script")).type="text/javascript",p.crossOrigin="anonymous",p.async=!0,p.src=s.api_host.replace(".i.posthog.com","-assets.i.posthog.com")+"/static/array.js",(r=t.getElementsByTagName("script")[0]).parentNode.insertBefore(p,r);var u=e;for(void 0!==a?u=e[a]=[]:a="posthog",u.people=u.people||[],u.toString=function(t){var e="posthog";return"posthog"!==a&&(e+="."+a),t||(e+=" (stub)"),e},u.people.toString=function(){return u.toString(1)+".people (stub)"},o="init capture register register_once register_for_session unregister opt_out_capturing has_opted_out_capturing opt_in_capturing reset isFeatureEnabled getFeatureFlag getFeatureFlagPayload reloadFeatureFlags group identify setPersonProperties setPersonPropertiesForFlags resetPersonPropertiesForFlags setGroupPropertiesForFlags resetGroupPropertiesForFlags resetGroups onFeatureFlags addFeatureFlagsHandler onSessionId getSurveys getActiveMatchingSurveys renderSurvey canRenderSurvey getNextSurveyStep".split(" "),n=0;n<o.length;n++)g(u,o[n]);e._i.push([i,s,a])},e.__SV=1)}(document,window.posthog||[]);

    // --- Init with Odoo-specific config ---
    window.posthog.init(POSTHOG_KEY, {
        api_host: POSTHOG_HOST,
        autocapture: false,  // Odoo DOM is complex — skip autocapture
        capture_pageview: false,  // We'll track Odoo navigation manually
        capture_pageleave: true,
        capture_performance: true,
        respect_dnt: false,  // Internal tool — always track
        enable_recording_console_log: true,
        session_recording: {
            maskAllInputs: true,
            maskInputOptions: { password: true },
            recordCanvas: false,
            recordCrossOriginIframes: false,
            maskTextSelector: ".o_data_cell.o_monetary_cell",  // Mask financial data
        },
        loaded: function (posthog) {
            // Identify by Odoo user
            const login = session.username || session.login || "unknown";
            const userId = session.uid || 0;
            const userName = session.name || login;

            posthog.identify("odoo-" + userId, {
                email: login.includes("@") ? login : undefined,
                name: userName,
                odoo_user: login,
                odoo_uid: userId,
                odoo_is_admin: session.is_admin || false,
                odoo_is_system: session.is_system || false,
                app: "odoo-backend",
                odoo_url: window.location.origin,
            });

            posthog.register({
                app: "odoo-backend",
                app_version: session.server_version || "19.0",
                odoo_user: login,
            });

            console.log("[PostHog] Odoo backend tracking active for", login);
        },
    });

    // --- Track Odoo RPC errors ---
    _patchRpcErrors();

    // --- Track JS errors ---
    _trackJsErrors();

    // --- Track Odoo action navigation ---
    _trackNavigation();
}

/**
 * Intercept Odoo RPC errors to capture them in PostHog.
 */
function _patchRpcErrors() {
    // Odoo uses the bus to broadcast RPC errors
    // We'll also patch the global error handler for uncaught RPC failures
    const origFetch = browser.fetch;
    browser.fetch = async function (...args) {
        const url = typeof args[0] === "string" ? args[0] : args[0]?.url || "";
        const startTime = Date.now();

        try {
            const resp = await origFetch.apply(this, args);
            const duration = Date.now() - startTime;

            // Track failed RPC calls
            if (!resp.ok && url.includes("/web/") || url.includes("/jsonrpc")) {
                let errorDetail = "";
                try {
                    const clone = resp.clone();
                    const body = await clone.text();
                    const parsed = JSON.parse(body);
                    errorDetail = parsed?.error?.data?.message || parsed?.error?.message || body.slice(0, 500);
                } catch { /* ignore parse errors */ }

                window.posthog?.capture("odoo_rpc_error", {
                    rpc_url: url,
                    rpc_method: args[1]?.method || "POST",
                    status_code: resp.status,
                    error_detail: errorDetail,
                    duration_ms: duration,
                    page_url: window.location.href,
                    odoo_action: _getCurrentAction(),
                });
            }

            // Track slow RPC (> 10s)
            if (duration > 10000 && (url.includes("/web/") || url.includes("/jsonrpc"))) {
                window.posthog?.capture("odoo_rpc_slow", {
                    rpc_url: url,
                    duration_ms: duration,
                    status_code: resp.status,
                    page_url: window.location.href,
                    odoo_action: _getCurrentAction(),
                });
            }

            return resp;
        } catch (err) {
            const duration = Date.now() - startTime;
            window.posthog?.capture("odoo_rpc_network_error", {
                rpc_url: url,
                error_message: err?.message || "Network error",
                duration_ms: duration,
                page_url: window.location.href,
                odoo_action: _getCurrentAction(),
            });
            throw err;
        }
    };
}

/**
 * Capture JS errors and unhandled promise rejections.
 */
function _trackJsErrors() {
    window.addEventListener("error", (event) => {
        window.posthog?.capture("$exception", {
            $exception_type: event.error?.name || "Error",
            $exception_message: event.message,
            $exception_source: event.filename,
            $exception_lineno: event.lineno,
            $exception_colno: event.colno,
            $exception_stack_trace_raw: event.error?.stack || null,
            app: "odoo-backend",
            page_url: window.location.href,
            odoo_action: _getCurrentAction(),
        });
    });

    window.addEventListener("unhandledrejection", (event) => {
        const reason = event.reason;
        window.posthog?.capture("$exception", {
            $exception_type: reason?.name || "UnhandledPromiseRejection",
            $exception_message: reason?.message || String(reason),
            $exception_stack_trace_raw: reason?.stack || null,
            $exception_handled: false,
            app: "odoo-backend",
            page_url: window.location.href,
            odoo_action: _getCurrentAction(),
        });
    });
}

/**
 * Track Odoo web client navigation (action changes).
 */
function _trackNavigation() {
    let lastHash = window.location.hash;

    // Odoo 19 uses hash-based routing (#action=xxx&model=yyy)
    window.addEventListener("hashchange", () => {
        const newHash = window.location.hash;
        if (newHash !== lastHash) {
            lastHash = newHash;
            window.posthog?.capture("$pageview", {
                $current_url: window.location.href,
                odoo_action: _getCurrentAction(),
                odoo_model: _getHashParam("model"),
                odoo_view_type: _getHashParam("view_type"),
            });
        }
    });
}

/**
 * Extract the current Odoo action from the URL hash.
 */
function _getCurrentAction() {
    return _getHashParam("action") || "unknown";
}

function _getHashParam(key) {
    const hash = window.location.hash.replace(/^#/, "");
    const params = new URLSearchParams(hash);
    return params.get(key) || "";
}

// --- Register as a service that boots on startup ---
registry.category("services").add("mint_posthog", {
    start() {
        // Boot after a short delay so Odoo session is fully loaded
        setTimeout(bootPostHog, 2000);
    },
});
