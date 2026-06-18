/** @odoo-module **/

/**
 * Daisy error handler — non-blocking.
 *
 * Instead of taking over Odoo's blocking "Oops!" modal, genuine server/client
 * crashes are SUPPRESSED and surfaced as a small, dismissable corner toast,
 * while the full error context (user name, email, uid, records viewed,
 * traceback) is sent to Daisy ASYNCHRONOUSLY for analysis. A "Technical
 * details" button on the toast re-opens the original Odoo error dialog for
 * developers who still need the raw traceback.
 *
 * Intentional business messages — UserError / ValidationError /
 * RedirectWarning, anything with a dedicated dialog or notification,
 * connection-lost, request-too-large (413), third-party-script and
 * asset-loading errors — are deferred to Odoo's own handlers untouched.
 *
 * Implemented as an entry in the "error_handlers" registry (sequence 96, just
 * ahead of core's rpcErrorHandler) rather than by patching the dialog
 * components, so the heavy modal never renders in the first place (no flash).
 */

import { registry } from "@web/core/registry";
import {
    rpc,
    RPCError,
    ConnectionLostError,
    RequestEntityTooLargeError,
} from "@web/core/network/rpc";
import {
    UncaughtError,
    ThirdPartyScriptError,
    HTMLElementLoadingError,
} from "@web/core/errors/error_service";
import { RPCErrorDialog } from "@web/core/errors/error_dialogs";
import { user } from "@web/core/user";
import { session } from "@web/session";

const errorHandlerRegistry = registry.category("error_handlers");
const errorDialogRegistry = registry.category("error_dialogs");
const errorNotificationRegistry = registry.category("error_notifications");

const DEFAULT_API_URL =
    "https://daisy.plus/api/v1/prediction/af2db2cc-f48c-4ca8-b1a9-36b156ec06cf";

// ---------------------------------------------------------------------------
// Config — loaded once at startup so the error handler stays fully synchronous
// (the registry loop expects a synchronous boolean from each handler).
// Until it resolves, or if disabled, we defer to Odoo's default behavior so an
// error is never silently swallowed.
// ---------------------------------------------------------------------------
let _config = null; // { apiUrl, enabled } once resolved; null until then

async function loadConfig() {
    try {
        // ir.config_parameter is admin-only; daisy.error.handler.config
        // reads it with sudo server-side so non-admin users get the real
        // values instead of an AccessError (maintenance request #522).
        const config = await rpc("/web/dataset/call_kw", {
            model: "daisy.error.handler.config",
            method: "get_config",
            args: [],
            kwargs: {},
        });
        _config = {
            apiUrl: (config && config.api_url) || DEFAULT_API_URL,
            enabled: !config || config.enabled !== false,
        };
    } catch (_e) {
        _config = { apiUrl: DEFAULT_API_URL, enabled: true };
    }
}

// Warm the cache as soon as the asset loads.
loadConfig();

// ---------------------------------------------------------------------------
// Context gathering
// ---------------------------------------------------------------------------
function getUserContext() {
    const ctx = {
        name: "Unknown",
        email: "",
        uid: "",
        db: "",
        url: window.location.href,
        timestamp: new Date().toISOString(),
    };
    try {
        ctx.name = user.name || ctx.name;
        ctx.email = user.login || ""; // login is the email address on this instance
        ctx.uid = user.userId || "";
    } catch (_e) {
        /* ignore */
    }
    try {
        ctx.db = session.db || "";
    } catch (_e) {
        /* ignore */
    }
    return ctx;
}

/**
 * Capture the record the user is currently looking at plus the breadcrumb
 * trail of records navigated to get there. The breadcrumbs are read from the
 * DOM so they match exactly what the user sees.
 */
function getRecordsViewed(env) {
    const out = { current: null, breadcrumbs: [] };
    try {
        const cc = env.services.action && env.services.action.currentController;
        if (cc) {
            const p = cc.props || {};
            const model = p.resModel || (cc.action && cc.action.res_model) || "";
            if (model) {
                out.current = {
                    model: model,
                    res_id: p.resId || null,
                    res_ids: p.resIds || (p.resId ? [p.resId] : []),
                };
            }
        }
    } catch (_e) {
        /* ignore */
    }
    try {
        const nodes = document.querySelectorAll(
            ".o_breadcrumb .breadcrumb-item, .o_breadcrumb .o_last_breadcrumb_item"
        );
        out.breadcrumbs = Array.from(nodes)
            .map((n) => (n.textContent || "").trim())
            .filter(Boolean);
    } catch (_e) {
        /* ignore */
    }
    return out;
}

/**
 * Build the Daisy payload: a human-readable question with all context inline
 * (guaranteed to reach the LLM) plus a structured `vars` object in case the
 * Daisy flow consumes variables directly.
 */
function buildPayload(env, error, originalError) {
    const ctx = getUserContext();
    const records = getRecordsViewed(env);
    const oe = originalError || {};

    const err = {
        name: oe.name || (error && error.name) || "",
        message: oe.message || (error && error.message) || "",
        exceptionName: oe.exceptionName || "",
        model: oe.model || (records.current && records.current.model) || "",
        traceback: (error && error.traceback) || oe.traceback || "",
    };

    const lines = [
        "An Odoo user hit an error. Analyze it and reply with a short, friendly explanation plus concrete next steps.",
        "",
        "User: " + ctx.name + (ctx.email ? " <" + ctx.email + ">" : ""),
        "User ID: " + ctx.uid,
        "Database: " + ctx.db,
        "URL: " + ctx.url,
        "Time: " + ctx.timestamp,
    ];
    if (records.current) {
        lines.push(
            "Record open: " +
                records.current.model +
                (records.current.res_id ? "#" + records.current.res_id : "")
        );
    }
    if (records.breadcrumbs.length) {
        lines.push("Records viewed (breadcrumbs): " + records.breadcrumbs.join(" › "));
    }
    lines.push("");
    if (err.exceptionName) lines.push("Exception: " + err.exceptionName);
    if (err.name) lines.push("Error: " + err.name);
    if (err.message) lines.push("Message: " + err.message);
    if (err.model) lines.push("Model: " + err.model);
    if (err.traceback) {
        lines.push("", "Traceback:", String(err.traceback).slice(0, 4000));
    }

    return {
        question: lines.join("\n"),
        vars: {
            user: ctx.name,
            email: ctx.email,
            user_id: ctx.uid,
            db: ctx.db,
            url: ctx.url,
            timestamp: ctx.timestamp,
            records_viewed: records,
            error: {
                name: err.name,
                message: err.message,
                exception_name: err.exceptionName,
                model: err.model,
            },
        },
    };
}

async function sendToDaisy(payload, apiUrl) {
    const res = await fetch(apiUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: payload.question, vars: payload.vars }),
    });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    return data.text || data.answer || "No response.";
}

// ---------------------------------------------------------------------------
// Presentation
// ---------------------------------------------------------------------------

/** Re-open the original Odoo error dialog (escape hatch for developers). */
function reopenTechnicalDialog(env, error, originalError) {
    try {
        const oe = originalError || {};
        const host =
            error && error.event && error.event.target && error.event.target.location
                ? error.event.target.location.host
                : undefined;
        env.services.dialog.add(RPCErrorDialog, {
            traceback: (error && error.traceback) || oe.traceback || "",
            message: oe.message || (error && error.message) || "",
            name: oe.name || (error && error.name) || "",
            exceptionName: oe.exceptionName,
            data: oe.data,
            subType: oe.subType,
            code: oe.code,
            type: oe.type,
            serverHost: host,
            model: oe.model,
        });
    } catch (_e) {
        /* ignore */
    }
}

function handleWithDaisy(env, error, originalError) {
    const notification = env.services.notification;
    const payload = buildPayload(env, error, originalError);

    // Small, non-blocking "working on it" toast.
    let closeLoading = () => {};
    try {
        closeLoading = notification.add("Daisy is reviewing this error…", {
            title: "🌼 Daisy",
            type: "info",
            sticky: true,
        });
    } catch (_e) {
        /* ignore */
    }

    const detailsButton = {
        name: "Technical details",
        onClick: () => reopenTechnicalDialog(env, error, originalError),
    };

    // Fire-and-forget — never block the UI on the network call.
    sendToDaisy(payload, _config.apiUrl)
        .then((answer) => {
            try {
                closeLoading();
            } catch (_e) {
                /* ignore */
            }
            notification.add(answer, {
                title: "🌼 Daisy",
                type: "warning",
                sticky: true,
                buttons: [detailsButton],
            });
        })
        .catch(() => {
            try {
                closeLoading();
            } catch (_e) {
                /* ignore */
            }
            notification.add(
                payload.vars.error.message ||
                    "Something went wrong. Daisy couldn't be reached for analysis.",
                {
                    title: "🌼 Daisy",
                    type: "warning",
                    sticky: true,
                    buttons: [detailsButton],
                }
            );
        });
}

// ---------------------------------------------------------------------------
// Registry handler
// ---------------------------------------------------------------------------

/**
 * Suppress the blocking "Oops!" dialog for genuine crashes and surface a small
 * Daisy toast instead. Everything that has a dedicated dialog/notification or
 * its own core handler is deferred (return false).
 */
function daisyErrorHandler(env, error, originalError) {
    // Not ready yet or explicitly disabled → let Odoo behave normally.
    if (!_config || !_config.enabled) {
        return false;
    }
    // Only act on uncaught wrappers (mirrors core handlers' first guard).
    if (!(error instanceof UncaughtError)) {
        return false;
    }

    if (originalError instanceof RPCError) {
        // Defer business exceptions that have a dedicated dialog/notification.
        if (originalError.Component) {
            return false;
        }
        const exc = originalError.exceptionName;
        if (exc && (errorNotificationRegistry.contains(exc) || errorDialogRegistry.contains(exc))) {
            return false;
        }
        const exClass =
            originalError.data && originalError.data.context
                ? originalError.data.context.exception_class
                : undefined;
        if (exClass && errorDialogRegistry.contains(exClass)) {
            return false;
        }
        // Genuine server crash (would otherwise show the fallback RPCErrorDialog).
        try {
            error.unhandledRejectionEvent.preventDefault();
        } catch (_e) {
            /* ignore */
        }
        handleWithDaisy(env, error, originalError);
        return true;
    }

    // Non-RPC errors that have their own dedicated core handlers → defer.
    if (
        originalError instanceof ConnectionLostError ||
        originalError instanceof RequestEntityTooLargeError ||
        originalError instanceof HTMLElementLoadingError ||
        error instanceof ThirdPartyScriptError
    ) {
        return false;
    }

    // Generic uncaught client-side crash (would otherwise show ErrorDialog).
    handleWithDaisy(env, error, originalError);
    return true;
}

// Register once even if both daisy_error_handler and daisydo_agents ship this file.
if (!errorHandlerRegistry.contains("daisyErrorHandler")) {
    errorHandlerRegistry.add("daisyErrorHandler", daisyErrorHandler, { sequence: 96 });
}
