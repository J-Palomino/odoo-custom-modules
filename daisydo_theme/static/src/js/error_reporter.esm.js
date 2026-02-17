/** @odoo-module **/

import { registry } from "@web/core/registry";
import { session } from "@web/session";

const DEDUP_WINDOW_MS = 5 * 60 * 1000; // 5 minutes
const _recentHashes = new Map(); // hash -> timestamp

function hashString(str) {
    let hash = 0;
    for (let i = 0; i < str.length; i++) {
        hash = ((hash << 5) - hash + str.charCodeAt(i)) | 0;
    }
    return hash;
}

function isDuplicate(traceback) {
    const hash = hashString(traceback);
    const now = Date.now();
    const lastSent = _recentHashes.get(hash);
    if (lastSent && now - lastSent < DEDUP_WINDOW_MS) {
        return true;
    }
    _recentHashes.set(hash, now);
    // Prune old entries
    for (const [k, ts] of _recentHashes) {
        if (now - ts > DEDUP_WINDOW_MS) {
            _recentHashes.delete(k);
        }
    }
    return false;
}

function reportErrorToDaisy(errorData) {
    const apiUrl = session.error_report_api_url;
    const apiKey = session.error_report_api_key;
    if (!apiUrl || !apiKey) {
        return; // Not configured
    }

    const traceback = errorData.traceback || errorData.message || "";
    if (!traceback) {
        return;
    }
    if (isDuplicate(traceback)) {
        return;
    }

    const db = session.db || "unknown";
    const sessionId = `odoo-errors-${db}`;
    const userLogin = session.username || session.login || "unknown";

    const payload = {
        session_id: sessionId,
        question: `[Auto-Report] Odoo error from user "${userLogin}":\n\n\`\`\`\n${traceback}\n\`\`\`\n\nPlease analyze this error and suggest a fix.`,
    };

    // Fire-and-forget — never block the UI
    fetch(`${apiUrl}/api/predict`, {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${apiKey}`,
        },
        body: JSON.stringify(payload),
    }).catch(() => {
        // Silently ignore — error reporting must never cause errors
    });
}

function errorReporterHandler(env, error, originalError) {
    // Extract traceback from RPC errors
    const data = originalError?.data || error?.data || {};
    const traceback = data.debug || data.traceback || "";

    if (traceback) {
        reportErrorToDaisy({ traceback });
    } else if (originalError?.message) {
        // Client-side JS errors — only report if they look like real errors
        const msg = originalError.message;
        if (msg.length > 20 && !msg.includes("ResizeObserver")) {
            reportErrorToDaisy({ message: msg, traceback: msg });
        }
    }

    // Return nothing so the normal error dialog still shows
}

registry.category("error_handlers").add("daisy_error_reporter", errorReporterHandler);
