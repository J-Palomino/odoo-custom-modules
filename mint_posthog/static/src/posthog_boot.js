/** @odoo-module **/
/**
 * PostHog Analytics for Odoo Backend
 * ===================================
 * Answers "who hit which error, on which screen, with what server traceback"
 * so a user saying "Odoo is broken for me" becomes a searchable question.
 *
 * Dedicated "LetsGoMint" PostHog project (544449) - separate from the
 * MintDeals2.0 storefront project so Odoo backend data stays isolated.
 * Filter by person property `odoo_user` or `app: "odoo-backend"`.
 *
 * ---------------------------------------------------------------------------
 * Every seam below was verified against the LIVE production asset bundle
 * (letsgomint.us, web.assets_backend, server 19.0-20260609) rather than
 * assumed. Three of them had been wrong since the module was written:
 *
 *  1. IDENTITY. `@web/core/user` destructures the session and then DELETES
 *     `session.uid`, `session.username`, `session.name`, `session.is_admin`
 *     and `session.is_system` from it at module-load time. Reading those keys
 *     afterwards yields undefined, which is why every event for 14 days
 *     landed on distinct_id "odoo-0" with odoo_user "unknown". Identity now
 *     comes from the `user` object, which is the only place it survives.
 *
 *  2. RPC. Odoo's `rpc()` builds requests with `browser.XMLHttpRequest`, NOT
 *     `fetch`. The old `browser.fetch` monkey-patch therefore captured zero
 *     Odoo RPC calls. We now subscribe to `rpcBus` ("RPC:REQUEST" /
 *     "RPC:RESPONSE"), which is the transport-agnostic seam core itself uses,
 *     and which carries the server traceback in `error.data.debug`.
 *
 *  3. NAVIGATION. Odoo 19 routes on real paths (/odoo/action-123/45) via
 *     `routerBus` "ROUTE_CHANGE"; it does not use hash routing. The old
 *     `hashchange` listener never fired, so `odoo_action` was "unknown" on
 *     every single captured error and no pageviews were recorded.
 *
 * Telemetry must never break the UI: every handler is wrapped, and every
 * failure here is swallowed.
 * ---------------------------------------------------------------------------
 */

import { session } from "@web/session";
import { user } from "@web/core/user";
import { rpcBus, RPCError } from "@web/core/network/rpc";
import { router, routerBus } from "@web/core/browser/router";

const POSTHOG_KEY = "phc_pn6qyCiqURG5TQodSwspowqCYQiKGz92tD3N2GeCjRBT";
const POSTHOG_HOST = "https://us.i.posthog.com";

/** An RPC slower than this is worth knowing about. */
const SLOW_RPC_MS = 10000;
/** Safety valve: in-flight RPC bookkeeping never grows unbounded. */
const MAX_PENDING_RPC = 500;
/** Identical problems inside this window collapse into one event. */
const DEDUPE_WINDOW_MS = 10000;
/** Hard ceiling on captured problems per minute, so a render loop can't flood. */
const RATE_LIMIT_MAX = 40;
const RATE_LIMIT_WINDOW_MS = 60000;

const SESSION_EXPIRED = "odoo.http.SessionExpiredException";

/**
 * Server exceptions that are deliberate business messages rather than bugs.
 * Verified against the names the web client itself references.
 */
const BUSINESS_EXCEPTIONS = new Set([
    "odoo.exceptions.UserError",
    "odoo.exceptions.ValidationError",
    "odoo.exceptions.AccessError",
    "odoo.exceptions.AccessDenied",
    "odoo.exceptions.MissingError",
    "odoo.exceptions.RedirectWarning",
    "odoo.exceptions.Warning",
]);

/** Browser noise that is never actionable. */
const IGNORED_ERRORS = [
    /ResizeObserver loop (completed with undelivered notifications|limit exceeded)/i,
    /^Script error\.?$/i,
];

let _booted = false;

// ---------------------------------------------------------------------------
// Rate limiting / de-duplication
// ---------------------------------------------------------------------------

let _windowStart = 0;
let _windowCount = 0;
let _dropped = 0;
const _lastSeen = new Map();

/**
 * Decide whether a problem keyed by `key` should be captured. Suppressed
 * events are counted and the running total rides along on the next event that
 * does get through, so a flood is visible in PostHog instead of silent.
 */
function allow(key) {
    const now = Date.now();
    if (now - _windowStart > RATE_LIMIT_WINDOW_MS) {
        _windowStart = now;
        _windowCount = 0;
    }
    const last = _lastSeen.get(key);
    if (last !== undefined && now - last < DEDUPE_WINDOW_MS) {
        _dropped++;
        return false;
    }
    if (_windowCount >= RATE_LIMIT_MAX) {
        _dropped++;
        return false;
    }
    if (_lastSeen.size > 200) {
        _lastSeen.clear();
    }
    _lastSeen.set(key, now);
    _windowCount++;
    return true;
}

function isIgnored(message) {
    const text = String(message || "");
    return IGNORED_ERRORS.some((re) => re.test(text));
}

// ---------------------------------------------------------------------------
// Context
// ---------------------------------------------------------------------------

/**
 * Where the user actually is. `router.current` is populated from the path
 * (PATH_KEYS = resId, action, active_id, model), so this stays correct across
 * Odoo 19's real-URL navigation.
 */
function odooContext() {
    const state = (router && router.current) || {};
    return {
        odoo_action: state.action === undefined || state.action === null ? "none" : String(state.action),
        odoo_model: state.model || "",
        odoo_res_id: state.resId === undefined ? null : state.resId,
        odoo_active_id: state.active_id === undefined ? null : state.active_id,
        page_url: window.location.href,
        page_path: window.location.pathname,
    };
}

/**
 * Identity, read from `@web/core/user` because the session keys are deleted
 * during core boot (see header note 1). `activeCompany` is normally a company
 * record but degrades to a bare id in one core fallback path, so handle both -
 * on a 59-company install, knowing which company the user was in is often the
 * whole answer.
 */
function identity() {
    const login = user.login || "unknown";
    const company = user.activeCompany;
    const isRecord = company && typeof company === "object";
    return {
        odoo_uid: user.userId === undefined ? null : user.userId,
        odoo_user: login,
        odoo_user_name: user.name || login,
        odoo_is_admin: !!user.isAdmin,
        odoo_is_system: !!user.isSystem,
        odoo_is_internal: !!user.isInternalUser,
        odoo_company_id: isRecord ? company.id : typeof company === "number" ? company : null,
        odoo_company: isRecord ? company.name || "" : "",
        app: "odoo-backend",
        app_version: session.server_version || "",
    };
}

// ---------------------------------------------------------------------------
// Capture helpers
// ---------------------------------------------------------------------------

function capture(event, props) {
    const ph = window.posthog;
    if (!ph || typeof ph.capture !== "function") {
        return;
    }
    try {
        const payload = Object.assign({}, odooContext(), props || {});
        if (_dropped) {
            payload.suppressed_since_last = _dropped;
            _dropped = 0;
        }
        ph.capture(event, payload);
    } catch (_e) {
        /* telemetry must never break the UI */
    }
}

/**
 * Send an exception in the shape PostHog's Error Tracking groups on. Prefers
 * the SDK's own captureException (it builds a real stack trace); falls back to
 * a hand-built $exception_list so issues still group by type + message.
 */
function captureException(error, extra, handled) {
    const ph = window.posthog;
    if (!ph) {
        return;
    }
    const props = Object.assign({}, odooContext(), extra || {});
    try {
        if (typeof ph.captureException === "function" && error instanceof Error) {
            ph.captureException(error, props);
            return;
        }
    } catch (_e) {
        /* fall through to the manual shape */
    }
    const type = (error && error.name) || props.$exception_type || "Error";
    const value = (error && error.message) || props.$exception_message || String(error);
    capture(
        "$exception",
        Object.assign(
            {
                $exception_type: type,
                $exception_message: value,
                $exception_handled: !!handled,
                $exception_stack_trace_raw: (error && error.stack) || null,
                $exception_list: [
                    {
                        type: type,
                        value: value,
                        mechanism: { handled: !!handled, synthetic: false },
                    },
                ],
            },
            props
        )
    );
}

// ---------------------------------------------------------------------------
// RPC instrumentation
// ---------------------------------------------------------------------------

/**
 * RPC:RESPONSE does not carry the url, so requests are held by id until their
 * response arrives and then dropped.
 */
const _pending = new Map();

function onRpcRequest(ev) {
    try {
        const detail = ev.detail || {};
        const data = detail.data || {};
        const params = data.params || {};
        if (_pending.size > MAX_PENDING_RPC) {
            _pending.clear();
        }
        _pending.set(data.id, {
            startedAt: Date.now(),
            url: detail.url || "",
            model: params.model || "",
            method: params.method || "",
        });
    } catch (_e) {
        /* ignore */
    }
}

function onRpcResponse(ev) {
    try {
        const detail = ev.detail || {};
        const data = detail.data || {};
        const request = _pending.get(data.id);
        _pending.delete(data.id);

        const error = detail.error;
        const duration = request ? Date.now() - request.startedAt : null;
        const base = {
            rpc_url: request ? request.url : "",
            rpc_model: (request && request.model) || (error && error.model) || "",
            rpc_method: (request && request.method) || "",
            duration_ms: duration,
        };

        if (!error) {
            if (duration !== null && duration > SLOW_RPC_MS) {
                if (allow("slow:" + base.rpc_model + "." + base.rpc_method)) {
                    capture("odoo_rpc_slow", base);
                }
            }
            return;
        }

        const exceptionName = error.exceptionName || error.name || "";
        const errorData = error.data || {};

        // Being logged out mid-task is the most common thing users actually
        // hit, and it is not a crash - give it its own event so it can be
        // trended separately instead of drowning the real errors.
        if (exceptionName === SESSION_EXPIRED) {
            if (allow("session_expired")) {
                capture("odoo_session_expired", base);
            }
            return;
        }

        const isBusiness = BUSINESS_EXCEPTIONS.has(exceptionName);
        const key = "rpc:" + exceptionName + ":" + base.rpc_model + "." + base.rpc_method;
        if (!allow(key)) {
            return;
        }

        capture(
            "odoo_rpc_error",
            Object.assign({}, base, {
                exception_name: exceptionName,
                error_message: error.message || errorData.message || "",
                error_code: error.code === undefined ? null : error.code,
                error_sub_type: error.subType || "",
                // The Python traceback. This is the single most useful field
                // here and the old implementation never captured it.
                server_traceback: String(errorData.debug || "").slice(0, 8000),
                is_business_error: isBusiness,
            })
        );

        // Genuine server crashes also become Error Tracking issues, so they
        // group and can be assigned. Business errors deliberately do not.
        if (!isBusiness && error instanceof Error) {
            captureException(error, Object.assign({}, base, {
                exception_name: exceptionName,
                server_traceback: String(errorData.debug || "").slice(0, 8000),
            }), true);
        }
    } catch (_e) {
        /* ignore */
    }
}

// ---------------------------------------------------------------------------
// Uncaught client errors
// ---------------------------------------------------------------------------

function onWindowError(event) {
    try {
        if (isIgnored(event && event.message)) {
            return;
        }
        const error = event && event.error;
        const message = (event && event.message) || (error && error.message) || "Unknown error";
        if (!allow("js:" + message)) {
            return;
        }
        captureException(
            error,
            {
                $exception_type: (error && error.name) || "Error",
                $exception_message: message,
                $exception_source: event && event.filename,
                $exception_lineno: event && event.lineno,
                $exception_colno: event && event.colno,
            },
            false
        );
    } catch (_e) {
        /* ignore */
    }
}

function onUnhandledRejection(event) {
    try {
        const reason = event && event.reason;
        // RPC failures already arrive through rpcBus with far better context
        // (model, method, server traceback) - do not double-count them.
        if (reason instanceof RPCError) {
            return;
        }
        const message = (reason && reason.message) || String(reason);
        if (isIgnored(message)) {
            return;
        }
        if (!allow("rejection:" + message)) {
            return;
        }
        captureException(
            reason instanceof Error ? reason : null,
            {
                $exception_type: (reason && reason.name) || "UnhandledPromiseRejection",
                $exception_message: message,
            },
            false
        );
    } catch (_e) {
        /* ignore */
    }
}

// ---------------------------------------------------------------------------
// Navigation
// ---------------------------------------------------------------------------

let _lastPageUrl = "";

function capturePageview(reason) {
    try {
        const url = window.location.href;
        if (url === _lastPageUrl) {
            return;
        }
        _lastPageUrl = url;
        capture("$pageview", { $current_url: url, nav_reason: reason });
    } catch (_e) {
        /* ignore */
    }
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

function boot() {
    if (_booted || typeof window === "undefined") {
        return;
    }
    _booted = true;

    // PostHog loader snippet. `captureException` is appended to the stubbed
    // method list so exceptions raised before the SDK finishes loading are
    // queued and replayed rather than dropped.
    !function(t,e){var o,n,p,r;e.__SV||(window.posthog=e,e._i=[],e.init=function(i,s,a){function g(t,e){var o=e.split(".");2==o.length&&(t=t[o[0]],e=o[1]),t[e]=function(){t.push([e].concat(Array.prototype.slice.call(arguments,0)))}}(p=t.createElement("script")).type="text/javascript",p.crossOrigin="anonymous",p.async=!0,p.src=s.api_host.replace(".i.posthog.com","-assets.i.posthog.com")+"/static/array.js",(r=t.getElementsByTagName("script")[0]).parentNode.insertBefore(p,r);var u=e;for(void 0!==a?u=e[a]=[]:a="posthog",u.people=u.people||[],u.toString=function(t){var e="posthog";return"posthog"!==a&&(e+="."+a),t||(e+=" (stub)"),e},u.people.toString=function(){return u.toString(1)+".people (stub)"},o="init capture captureException register register_once register_for_session unregister opt_out_capturing has_opted_out_capturing opt_in_capturing reset isFeatureEnabled getFeatureFlag getFeatureFlagPayload reloadFeatureFlags group identify setPersonProperties setPersonPropertiesForFlags resetPersonPropertiesForFlags setGroupPropertiesForFlags resetGroupPropertiesForFlags resetGroups onFeatureFlags addFeatureFlagsHandler onSessionId getSurveys getActiveMatchingSurveys renderSurvey canRenderSurvey getNextSurveyStep".split(" "),n=0;n<o.length;n++)g(u,o[n]);e._i.push([i,s,a])},e.__SV=1)}(document,window.posthog||[]);

    window.posthog.init(POSTHOG_KEY, {
        api_host: POSTHOG_HOST,
        autocapture: false, // Odoo's DOM is far too dynamic to be useful
        capture_pageview: false, // navigation is captured from routerBus instead
        capture_pageleave: true,
        capture_performance: true,
        respect_dnt: false, // internal tool
        enable_recording_console_log: true,
        session_recording: {
            maskAllInputs: true,
            maskInputOptions: { password: true },
            recordCanvas: false,
            recordCrossOriginIframes: false,
            // Keep money and customer PII out of replays.
            maskTextSelector: ".o_data_cell.o_monetary_cell, .o_field_monetary, .o_field_phone, .o_field_email",
        },
    });

    // Identity is registered synchronously rather than in the `loaded`
    // callback, so it is attached to everything - including events queued
    // before the SDK script finishes downloading.
    const who = identity();
    try {
        window.posthog.identify("odoo-" + (who.odoo_uid === null ? "anonymous" : who.odoo_uid), {
            email: who.odoo_user.includes("@") ? who.odoo_user : undefined,
            name: who.odoo_user_name,
            odoo_user: who.odoo_user,
            odoo_uid: who.odoo_uid,
            odoo_is_admin: who.odoo_is_admin,
            odoo_is_system: who.odoo_is_system,
            odoo_company: who.odoo_company,
            app: "odoo-backend",
            odoo_url: window.location.origin,
        });
        window.posthog.register(who);
    } catch (_e) {
        /* ignore */
    }

    window.addEventListener("error", onWindowError);
    window.addEventListener("unhandledrejection", onUnhandledRejection);
    rpcBus.addEventListener("RPC:REQUEST", onRpcRequest);
    rpcBus.addEventListener("RPC:RESPONSE", onRpcResponse);
    routerBus.addEventListener("ROUTE_CHANGE", () => capturePageview("route_change"));

    capturePageview("initial");
}

// Boot at module evaluation. `user` and `router` are both fully initialised by
// the time this runs (we import them), so there is nothing to wait for - and
// the old two-second delay meant errors thrown during startup, which are the
// worst ones, were never recorded at all.
//
// Wrapped because this runs inside the backend asset bundle: if analytics
// somehow throws here it must degrade to "no telemetry", never to "no web
// client".
try {
    boot();
} catch (_e) {
    // eslint-disable-next-line no-console
    console.warn("[PostHog] Odoo backend tracking failed to start", _e);
}

export const _test = { allow, isIgnored, identity, odooContext, onRpcResponse };
