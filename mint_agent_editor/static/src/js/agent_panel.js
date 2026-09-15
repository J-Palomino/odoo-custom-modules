/** @odoo-module **/
/**
 * Chat panel that sits beside the website builder and edits the page you are
 * looking at.
 *
 * The panel never asks the server to write the page. It applies the agent's
 * edit plan to the live editable DOM, so the change is visible at once and
 * becomes part of your unsaved edit; Odoo's Save button persists it through the
 * editor's own serialiser. That is what keeps an agent-inserted block as
 * editable as one you dragged in yourself — and it sidesteps the lost-update
 * race you get when a background write lands under an open editor.
 *
 * Written as a plain module rather than an OWL component on purpose: it hooks
 * only stable DOM, so it does not break when the editor's component internals
 * move between versions.
 */

import { rpc } from "@web/core/network/rpc";

const EDITABLE_ROOT = "#wrap";
const MAX_OUTLINE_BLOCKS = 40;

function editableRoot() {
    return document.querySelector(EDITABLE_ROOT);
}

function inEditMode() {
    // The editor sets this on <body>; checking the DOM rather than a JS import
    // keeps the panel working across editor refactors.
    return document.body.classList.contains("editor_enable")
        || document.body.classList.contains("editor_has_snippets")
        || !!document.querySelector(".o_we_website_top_actions");
}

/**
 * A compact census of the blocks on the page.
 *
 * Sent instead of the whole DOM: it keeps the prompt small, and it anchors the
 * agent's selectors to blocks that actually exist rather than ones it imagines.
 */
function pageOutline() {
    const root = editableRoot();
    if (!root) return [];
    const blocks = root.querySelectorAll("[data-snippet]");
    return Array.from(blocks).slice(0, MAX_OUTLINE_BLOCKS).map((el, i) => {
        const snippet = el.getAttribute("data-snippet");
        // Give every block an addressable selector, minting one if needed, so
        // the agent can always target a block unambiguously.
        if (!el.id) el.id = `agent_block_${i}_${snippet}`;
        const heading = el.querySelector("h1,h2,h3");
        return {
            selector: `#${el.id}`,
            snippet,
            name: el.getAttribute("data-name") || "",
            heading: heading ? heading.textContent.trim().slice(0, 80) : "",
            text: (el.textContent || "").trim().replace(/\s+/g, " ").slice(0, 120),
        };
    });
}

function pageContext() {
    const html = document.documentElement;
    return {
        url: window.location.pathname,
        website_id: Number(html.getAttribute("data-website-id")) || undefined,
        view_id: Number(html.getAttribute("data-view-xmlid-id")) || undefined,
        page_id: Number(html.getAttribute("data-main-object-id")) || undefined,
        main_object: html.getAttribute("data-main-object") || undefined,
    };
}

/**
 * Nudge the editor into noticing the change.
 *
 * The editor tracks edits with a MutationObserver, so a direct DOM change is
 * normally seen on its own; these are belt-and-braces for the cases where a
 * programmatic change does not trip the same path a keystroke does.
 */
function markDirty(el) {
    const target = el && el.closest ? (el.closest(".o_editable, [data-snippet]") || el) : el;
    if (!target) return;
    target.classList.add("o_dirty");
    target.dispatchEvent(new Event("input", { bubbles: true }));
    target.dispatchEvent(new Event("content_changed", { bubbles: true }));
}

async function applyOps(ops, log) {
    const root = editableRoot();
    if (!root) {
        log("No editable region on this page — open a website page in the editor.", "err");
        return { applied: 0, failed: ops.length };
    }
    let applied = 0, failed = 0;
    for (const op of ops) {
        try {
            if (op.op === "insert_snippet") {
                const res = await rpc("/mint_agent_editor/snippet", { name: op.snippet });
                if (res.error) throw new Error(res.error);
                const anchor = op.anchor ? root.querySelector(op.anchor) : null;
                const position = op.position || "append";
                if (position === "append" || !anchor) {
                    root.insertAdjacentHTML("beforeend", res.html);
                    markDirty(root.lastElementChild);
                } else {
                    anchor.insertAdjacentHTML(
                        position === "before" ? "beforebegin" : "afterend", res.html);
                    markDirty(anchor);
                }
                log(`inserted ${res.snippet} (${res.template})`, "ok");
                applied++;
                continue;
            }

            const el = root.querySelector(op.selector);
            if (!el) throw new Error(`no element matches ${op.selector}`);
            if (op.op === "set_text") {
                el.textContent = op.value ?? "";
            } else if (op.op === "set_html") {
                el.innerHTML = op.value ?? "";
            } else if (op.op === "set_attr") {
                el.setAttribute(op.name, op.value ?? "");
            } else if (op.op === "remove") {
                el.remove();
            } else {
                throw new Error(`unknown op ${op.op}`);
            }
            markDirty(el);
            log(`${op.op} on ${op.selector}`, "ok");
            applied++;
        } catch (err) {
            log(`${op.op || "op"} failed — ${err.message}`, "err");
            failed++;
        }
    }
    return { applied, failed };
}

class AgentPanel {
    constructor() {
        this.conversationId = null;
        this.undoStack = [];
        this.root = null;
    }

    mount() {
        if (document.querySelector(".mint-agent-panel")) return;
        const el = document.createElement("div");
        el.className = "mint-agent-panel";
        el.innerHTML = `
            <div class="mint-agent-head">
              <span class="mint-agent-title">Agent</span>
              <button class="mint-agent-undo" type="button" title="Undo last edit" disabled>Undo</button>
              <button class="mint-agent-close" type="button" title="Hide">–</button>
            </div>
            <div class="mint-agent-log"></div>
            <form class="mint-agent-form">
              <textarea class="mint-agent-input" rows="2"
                placeholder="Tell the agent what to change…"></textarea>
              <button type="submit" class="mint-agent-send">Send</button>
            </form>`;
        document.body.appendChild(el);
        this.root = el;

        this.logEl = el.querySelector(".mint-agent-log");
        this.inputEl = el.querySelector(".mint-agent-input");
        this.undoEl = el.querySelector(".mint-agent-undo");

        el.querySelector(".mint-agent-form").addEventListener("submit", (ev) => {
            ev.preventDefault();
            this.send();
        });
        this.inputEl.addEventListener("keydown", (ev) => {
            if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey)) {
                ev.preventDefault();
                this.send();
            }
        });
        el.querySelector(".mint-agent-close").addEventListener("click", () => {
            el.classList.toggle("mint-agent-collapsed");
        });
        this.undoEl.addEventListener("click", () => this.undo());

        if (!inEditMode()) {
            this.log("Not in edit mode. Click Edit to let the agent change this page.", "warn");
        }
    }

    log(text, kind = "msg") {
        const line = document.createElement("div");
        line.className = `mint-agent-line mint-agent-${kind}`;
        line.textContent = text;
        this.logEl.appendChild(line);
        this.logEl.scrollTop = this.logEl.scrollHeight;
    }

    /** Snapshot before every plan, so one click puts the page back. */
    pushUndo() {
        const root = editableRoot();
        if (!root) return;
        this.undoStack.push(root.innerHTML);
        if (this.undoStack.length > 10) this.undoStack.shift();
        this.undoEl.disabled = false;
    }

    undo() {
        const root = editableRoot();
        const prev = this.undoStack.pop();
        if (!root || prev === undefined) return;
        root.innerHTML = prev;
        markDirty(root);
        this.log("reverted last edit", "ok");
        this.undoEl.disabled = this.undoStack.length === 0;
    }

    async send() {
        const message = this.inputEl.value.trim();
        if (!message) return;
        this.inputEl.value = "";
        this.log(`you: ${message}`, "you");

        let res;
        try {
            res = await rpc("/mint_agent_editor/chat", {
                message,
                page: pageContext(),
                outline: pageOutline(),
                conversation_id: this.conversationId,
            });
        } catch (err) {
            this.log(`request failed — ${err.message || err}`, "err");
            return;
        }
        if (res.error) {
            this.log(res.error, "err");
            return;
        }
        this.conversationId = res.conversation_id || this.conversationId;
        this.log(`${res.agent || "agent"}: ${res.reply}`, "agent");

        if (!res.ops || !res.ops.length) return;
        if (!inEditMode()) {
            this.log(`${res.ops.length} change(s) proposed — enter edit mode to apply.`, "warn");
            return;
        }
        this.pushUndo();
        const { applied, failed } = await applyOps(res.ops, (t, k) => this.log(t, k));
        this.log(
            `applied ${applied}${failed ? `, ${failed} failed` : ""} — review, then Save.`,
            failed ? "warn" : "ok"
        );
    }
}

function start() {
    // Only for users who can actually edit. The controller enforces this too;
    // this just avoids showing a panel that could not do anything.
    if (!document.querySelector(".o_menu_systray .o_edit_website_container, .o_frontend_to_backend_edit_btn")
        && !inEditMode()) {
        return;
    }
    new AgentPanel().mount();
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
} else {
    start();
}
