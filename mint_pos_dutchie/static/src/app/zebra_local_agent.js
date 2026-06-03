/** @odoo-module **/

/**
 * Local print-agent transport (self-hosted, PrintNode-style).
 *
 * The POS PWA POSTs raw ZPL to a tiny agent running on the register at
 * http://127.0.0.1:<port> (see static/agent/mint_zebra_agent.py). The agent
 * prints to the OS-installed Zebra via the normal driver — no WebUSB / Zadig
 * driver swap, no cloud SaaS.
 *
 * Browsers allow an HTTPS page to call http://localhost (localhost is a
 * secure context); the agent returns the CORS + Private-Network-Access
 * headers that make the cross-origin localhost call succeed.
 */

const DEFAULT_URL = "http://127.0.0.1:17777";

function base(url) {
    return (url || DEFAULT_URL).replace(/\/+$/, "");
}

async function isAvailable(url, timeoutMs = 1500) {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
        const res = await fetch(base(url) + "/status", {
            method: "GET",
            signal: ctrl.signal,
        });
        if (!res.ok) {
            return false;
        }
        const body = await res.json();
        return !!body.ok;
    } catch {
        return false;
    } finally {
        clearTimeout(t);
    }
}

async function send(url, zpl, { token, printer } = {}) {
    const headers = { "Content-Type": "application/json" };
    if (token) {
        headers["X-Agent-Token"] = token;
    }
    const res = await fetch(base(url) + "/print", {
        method: "POST",
        headers,
        body: JSON.stringify({ zpl, printer: printer || undefined }),
    });
    if (!res.ok) {
        let detail = res.status;
        try {
            detail = (await res.json()).error || detail;
        } catch {
            /* ignore */
        }
        throw new Error("agent print failed: " + detail);
    }
    return res.json();
}

export const ZebraLocalAgent = { isAvailable, send, DEFAULT_URL };
