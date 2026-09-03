/** @odoo-module **/
/**
 * Style (SCSS) compilation failures -> PostHog
 * ============================================
 * Odoo surfaces a failed style compilation as a sticky *notification* plus a
 * `console.log` — see the `scss_error_display` service in
 * `web/static/src/core/assets`. It never throws, never rejects a promise, and
 * never reaches `window.onerror`, so neither posthog-js's own exception
 * capture nor the `window.addEventListener("error")` hooks in
 * `posthog_boot.js` can see it. A broken bundle can therefore sit in front of
 * every backend user without producing a single event.
 *
 * This service runs the same detection Odoo does — the asset compiler appends
 * a rule with the selector `css_error_message` to the bundle it could not
 * build, and puts the compiler output in that rule's `content` — and reports
 * it through `posthog.captureException`.
 *
 * `captureException` (not a hand-rolled `capture("$exception", …)`) is
 * deliberate: only the `$exception_list` payload it builds earns an
 * `$exception_issue_id` from PostHog, and the PostHog -> Odoo ticket cron
 * discards any exception without one.
 */

import { registry } from "@web/core/registry";
import { browser } from "@web/core/browser/browser";
import { getOrigin } from "@web/core/utils/urls";

const DETAIL_MAX_CHARS = 3000;
// posthog_boot.js boots posthog on a 2s delay and then pulls array.js over the
// network; `captureException` only exists once that has landed.
const POLL_INTERVAL_MS = 2000;
const POLL_MAX_ATTEMPTS = 15;

/**
 * Scan the same stylesheets Odoo's own handler scans and return the compile
 * error if one is present.
 *
 * @returns {{bundle: string, detail: string}|null}
 */
function findStyleCompilationError() {
    if (browser.location.origin === "null") {
        return null;
    }
    const origin = getOrigin();
    for (const sheet of [...document.styleSheets]) {
        const href = sheet.href;
        if (!href || !href.includes("/web") || !href.includes("/assets/")) {
            continue;
        }
        if (new URL(href, browser.location.origin).origin !== origin) {
            continue;
        }
        let cssRules;
        try {
            cssRules = sheet.cssRules;
        } catch {
            continue; // unreadable sheet — Odoo's handler skips these too
        }
        const lastRule = cssRules?.[cssRules.length - 1];
        if (lastRule?.selectorText !== "css_error_message") {
            continue;
        }
        const detail = String(lastRule.style?.content || "")
            .replaceAll("\\a", "\n")
            .replaceAll("\\*", "*")
            .replaceAll('\\"', '"')
            .trim();
        return { bundle: href, detail };
    }
    return null;
}

/**
 * First non-empty line of the compiler output. This lands in the exception
 * message, so it is what PostHog groups on — one issue per distinct breakage.
 */
function summarize(detail) {
    const line = detail
        .split("\n")
        .map((l) => l.trim())
        .find(Boolean);
    return line ? line.slice(0, 200) : "no compiler output in css_error_message";
}

function report({ bundle, detail }) {
    const props = {
        app: "odoo-backend",
        odoo_style_error: true,
        style_error_bundle: bundle,
        style_error_detail: detail.slice(0, DETAIL_MAX_CHARS),
        style_error_truncated: detail.length > DETAIL_MAX_CHARS,
        page_url: window.location.href,
    };

    const error = new Error(`Odoo style compilation failed: ${summarize(detail)}`);
    error.name = "OdooStyleCompilationError";
    window.posthog.captureException(error, props);

    // Plain event as well: `$exception` depends on PostHog error tracking to
    // group it, this one is queryable and alertable on its own.
    window.posthog.capture("odoo_style_compilation_failed", props);
}

/**
 * Poll for both halves of the problem: the stylesheets may still be loading
 * when the service starts, and posthog may not have finished booting.
 */
function pollAndReport() {
    let hit = null;
    let attempts = 0;

    const tick = () => {
        attempts++;
        hit = hit || findStyleCompilationError();
        if (hit && typeof window.posthog?.captureException === "function") {
            report(hit);
            return;
        }
        if (attempts >= POLL_MAX_ATTEMPTS) {
            if (hit) {
                console.warn(
                    "[mint_posthog] style compilation failure detected but posthog never loaded — not reported"
                );
            }
            return;
        }
        browser.setTimeout(tick, POLL_INTERVAL_MS);
    };

    tick();
}

registry.category("services").add("mint_posthog_scss_error", {
    start() {
        pollAndReport();
    },
});
