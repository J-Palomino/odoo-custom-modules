/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import {
    ErrorDialog,
    RPCErrorDialog,
    WarningDialog,
    RedirectWarningDialog,
} from "@web/core/errors/error_dialogs";
import { onMounted } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";

// Config cache — loaded once from ir.config_parameter via RPC
let _configCache = null;

async function getConfig() {
    if (_configCache) return _configCache;
    try {
        const result = await rpc("/web/dataset/call_kw", {
            model: "ir.config_parameter",
            method: "get_param",
            args: ["daisy.error_handler_api_url", ""],
            kwargs: {},
        });
        const enabled = await rpc("/web/dataset/call_kw", {
            model: "ir.config_parameter",
            method: "get_param",
            args: ["daisy.error_handler_enabled", "true"],
            kwargs: {},
        });
        _configCache = {
            apiUrl: result || "https://daisy.plus/api/v1/prediction/af2db2cc-f48c-4ca8-b1a9-36b156ec06cf",
            enabled: String(enabled).toLowerCase() !== "false",
        };
    } catch (_e) {
        _configCache = {
            apiUrl: "https://daisy.plus/api/v1/prediction/af2db2cc-f48c-4ca8-b1a9-36b156ec06cf",
            enabled: true,
        };
    }
    return _configCache;
}

function escapeHtml(str) {
    const d = document.createElement("div");
    d.textContent = str;
    return d.innerHTML;
}

/**
 * Gather current user and environment context from the Odoo session.
 */
function getUserContext() {
    try {
        const session = odoo && odoo.session_info ? odoo.session_info : {};
        return {
            user: session.name || session.username || "Unknown",
            login: session.username || session.user_login || "",
            uid: session.uid || "",
            url: window.location.href,
            pathname: window.location.pathname + window.location.hash,
            timestamp: new Date().toISOString(),
            db: session.db || "",
        };
    } catch (_e) {
        return {
            user: "Unknown",
            login: "",
            uid: "",
            url: window.location.href,
            pathname: window.location.pathname,
            timestamp: new Date().toISOString(),
            db: "",
        };
    }
}

/**
 * Build a question payload from error dialog props.
 * Includes traceback, model, exception type, user context, and URL —
 * everything the AI needs to give a useful response.
 */
function buildQuestion(props) {
    const parts = ["Odoo Error — analyze and give a concise, friendly explanation and steps to fix:"];

    // User & environment context
    const ctx = getUserContext();
    parts.push("User: " + ctx.user + (ctx.login ? " (" + ctx.login + ")" : ""));
    parts.push("URL: " + ctx.url);
    parts.push("Time: " + ctx.timestamp);
    if (ctx.db) parts.push("Database: " + ctx.db);

    // Error details
    if (props.title) parts.push("Title: " + props.title);
    if (props.name) parts.push("Error: " + props.name);
    if (props.exceptionName) parts.push("Exception: " + props.exceptionName);
    if (props.message) parts.push("Message: " + props.message);
    if (props.data && props.data.arguments) {
        parts.push("Detail: " + String(props.data.arguments[0] || "").slice(0, 2000));
    }
    if (props.model) parts.push("Model: " + props.model);
    if (props.data && props.data.debug) {
        parts.push("\nTraceback:\n" + String(props.data.debug).slice(0, 2000));
    }
    if (props.traceback) {
        parts.push("\nTraceback:\n" + String(props.traceback).slice(0, 1500));
    }
    return parts.join("\n");
}

async function callDaisy(question, apiUrl) {
    const res = await fetch(apiUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question }),
    });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    return data.text || "No response.";
}

/**
 * Replace the entire modal body with a Daisy AI response.
 * Technical details are hidden behind a collapsed toggle.
 */
function daisyOnMount(component) {
    onMounted(() => {
        try {
            const question = buildQuestion(component.props);
            if (question.length < 30) return;

            setTimeout(async () => {
                try {
                    const config = await getConfig();
                    if (!config.enabled) return;

                    // Find the dialog's modal body
                    const dialogs = document.querySelectorAll(".o_dialog:last-child .modal-body, .o_error_dialog .modal-body");
                    const body = dialogs[dialogs.length - 1];
                    if (!body) return;
                    if (body.querySelector(".daisy-error-replaced")) return;

                    // Save original content, then replace
                    const originalHtml = body.innerHTML;
                    const techId = "daisy-tech-" + Date.now();

                    body.innerHTML =
                        '<div class="daisy-error-replaced">' +
                        '  <div class="daisy-loading" style="display:flex;align-items:center;gap:10px;padding:16px;background:#eef2ff;border:1px solid #c7d2fe;border-radius:8px;">' +
                        '    <div class="spinner-border spinner-border-sm text-primary"></div>' +
                        '    <span style="color:#4338ca;font-weight:500;">Daisy is analyzing this error\u2026</span>' +
                        '  </div>' +
                        '</div>';

                    try {
                        const answer = await callDaisy(question, config.apiUrl);

                        const container = body.querySelector(".daisy-error-replaced");
                        container.innerHTML =
                            '<div style="padding:16px;background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;">' +
                            '  <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;">' +
                            '    <span class="badge text-bg-success" style="font-size:0.85em;">Daisy</span>' +
                            '  </div>' +
                            '  <div style="white-space:pre-wrap;font-size:0.92em;line-height:1.6;color:#15803d;">' +
                            escapeHtml(answer) +
                            '  </div>' +
                            '</div>' +
                            '<div style="margin-top:12px;">' +
                            '  <a href="#" onclick="document.getElementById(\'' + techId + '\').style.display=document.getElementById(\'' + techId + '\').style.display===\'none\'?\'block\':\'none\';return false;" ' +
                            '     style="font-size:0.8em;color:#94a3b8;text-decoration:none;">' +
                            '    \u25B6 Show technical details' +
                            '  </a>' +
                            '  <div id="' + techId + '" style="display:none;margin-top:8px;padding:12px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px;max-height:250px;overflow-y:auto;font-size:0.78em;">' +
                            originalHtml +
                            '  </div>' +
                            '</div>';
                    } catch (apiErr) {
                        body.querySelector(".daisy-error-replaced").innerHTML =
                            '<div style="padding:12px;background:#fef2f2;border:1px solid #fecaca;border-radius:8px;margin-bottom:12px;">' +
                            '  <small style="color:#991b1b;">Could not reach Daisy AI assistant.</small>' +
                            '</div>' +
                            originalHtml;
                    }
                } catch (e) {
                    console.warn("Daisy error handler:", e.message);
                }
            }, 50);
        } catch (e) {
            // Never break the error dialog itself
        }
    });
}

// Patch all four error dialog types
for (const Dialog of [ErrorDialog, RPCErrorDialog, WarningDialog, RedirectWarningDialog]) {
    try {
        patch(Dialog.prototype, {
            setup() {
                super.setup(...arguments);
                daisyOnMount(this);
            },
        });
    } catch (e) {
        console.warn("Daisy: failed to patch " + Dialog.name, e);
    }
}
