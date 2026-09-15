# -*- coding: utf-8 -*-
"""Endpoints backing the in-editor agent panel.

Two jobs, deliberately kept apart:

``/mint_agent_editor/chat`` relays a message to a Daisy agent together with the
page the user is actually looking at, and returns the agent's prose plus a
structured edit plan.

``/mint_agent_editor/snippet`` materialises a website snippet template into
builder-legal markup so the panel can insert a real block rather than a
hand-written lookalike.

Neither endpoint writes page content. The panel applies the plan to the open
editor's DOM and Odoo's own Save persists it — see the module description for
why writing arch_db under a live editor is a race nobody wins.
"""

import json
import logging
import re

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

# Which agent answers in the panel. An ir.config_parameter so the agent can be
# swapped without a deploy.
AGENT_CODE_PARAM = "mint_agent_editor.agent_code"
DEFAULT_AGENT_CODE = "alex-garcia"

EDITOR_GROUP = "website.group_website_designer"

# Snippet names collide across modules — mass_mailing ships s_cover, s_title,
# s_text_block and s_call_to_action too, with lower ids, so an unpinned lookup
# silently returns the EMAIL snippet. Pin the website namespace.
WEBSITE_SNIPPET_MODULES = ("website", "website_sale", "website_blog", "website_event")

SNIPPET_NAME_RE = re.compile(r"^s_[a-z0-9_]+$")

# The agent must answer with prose for the human AND a machine-readable plan.
# Agentflow V2 gives us no per-call vars, so this rides in the question text —
# the same constraint daisy_agent._build_context_prefix documents.
PLAN_PROTOCOL = """
[You are editing a live Odoo website page. Reply with a short sentence for the
human, then a fenced ```json block containing {"ops": [...]}.
Allowed ops, applied to the page in the user's editor:
  {"op":"set_text","selector":"<css>","value":"..."}
  {"op":"set_html","selector":"<css>","value":"<p>..</p>"}
  {"op":"insert_snippet","snippet":"s_features","anchor":"<css>","position":"after|before|append"}
  {"op":"remove","selector":"<css>"}
  {"op":"set_attr","selector":"<css>","name":"href","value":"/x"}
Selectors must match blocks listed in the page outline below. Emit no ops if
the request is a question rather than a change.]
""".strip()


def _extract_plan(text):
    """Pull the ops list out of an agent reply.

    Defensive on purpose: the model is instructed to fence the JSON but will
    sometimes inline it or wrap it in prose. A malformed plan yields no ops
    rather than an exception — the human still sees the reply.
    """
    if not text:
        return []
    candidates = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if not candidates:
        candidates = re.findall(r"(\{[^{}]*\"ops\"\s*:\s*\[.*?\]\s*\})", text, re.S)
    for blob in candidates:
        try:
            parsed = json.loads(blob)
        except ValueError:
            continue
        ops = parsed.get("ops")
        if isinstance(ops, list):
            return [op for op in ops if isinstance(op, dict) and op.get("op")]
    return []


def _strip_plan(text):
    """The prose half of the reply, without the JSON the human should not read."""
    return re.sub(r"```(?:json)?\s*\{.*?\}\s*```", "", text or "", flags=re.S).strip()


class MintAgentEditor(http.Controller):

    def _require_editor(self):
        if not request.env.user.has_group(EDITOR_GROUP):
            raise http.Forbidden("Website editor rights are required.")

    def _agent(self):
        code = (
            request.env["ir.config_parameter"].sudo().get_param(AGENT_CODE_PARAM)
            or DEFAULT_AGENT_CODE
        )
        agent = request.env["daisy.agent"].sudo().search(
            [("code", "=", code), ("active", "=", True)], limit=1
        )
        return agent

    @http.route("/mint_agent_editor/chat", type="json", auth="user", website=True)
    def chat(self, message=None, page=None, outline=None, conversation_id=None, **kw):
        """Relay one message plus the page under edit; return reply + edit plan.

        ``outline`` is the panel's census of the blocks currently on the page
        (selector + snippet + a text excerpt). Sending the outline rather than
        the whole DOM keeps the prompt small and, more importantly, keeps the
        agent's selectors anchored to blocks that actually exist.
        """
        self._require_editor()
        agent = self._agent()
        if not agent:
            return {"error": "No active agent is configured for the editor panel."}

        page = page or {}
        context_lines = [
            PLAN_PROTOCOL,
            "[Editing page: %s (website.page %s, ir.ui.view %s, website %s)]"
            % (
                page.get("url") or "?",
                page.get("page_id") or "?",
                page.get("view_id") or "?",
                page.get("website_id") or "?",
            ),
        ]
        if outline:
            context_lines.append("[Page outline: %s]" % json.dumps(outline)[:4000])

        prefix = agent._build_context_prefix(
            message=None,
            session_id="editor-%s" % request.env.user.id,
            page="\n".join(context_lines),
        )

        result = request.env["daisy.ai.service"].sudo()._call_daisy_api_for_agent(
            agent,
            (prefix or "") + (message or ""),
            conversation_id=conversation_id,
            session_id="editor-%s" % request.env.user.id,
        )
        reply = (result or {}).get("response") or ""
        if not reply:
            return {"error": (result or {}).get("error") or "The agent did not reply."}

        ops = _extract_plan(reply)
        _logger.info(
            "[agent-editor] user=%s page=%s ops=%s",
            request.env.user.login, page.get("url"), len(ops),
        )
        return {
            "reply": _strip_plan(reply) or reply,
            "ops": ops,
            "conversation_id": (result or {}).get("conversation_id"),
            "agent": agent.name,
        }

    @http.route("/mint_agent_editor/snippet", type="json", auth="user", website=True)
    def snippet(self, name=None, **kw):
        """Return builder-legal markup for one website snippet.

        Templates ship WITHOUT data-snippet/data-name — the editor stamps those
        when a block is dropped. Reproduce that, or the inserted block renders
        fine and cannot be selected.
        """
        self._require_editor()
        if not name or not SNIPPET_NAME_RE.match(name):
            return {"error": "Expected an s_-prefixed snippet name."}

        View = request.env["ir.ui.view"].sudo()
        views = View.search([("key", "=like", "%%.%s" % name), ("type", "=", "qweb")])
        chosen = None
        for module in WEBSITE_SNIPPET_MODULES:
            for view in views:
                if view.key == "%s.%s" % (module, name):
                    chosen = view
                    break
            if chosen:
                break
        if not chosen:
            return {"error": "No website-namespace template provides %s." % name}

        from lxml import etree

        root = etree.fromstring(chosen.arch)
        block = root if root.tag != "t" else next(
            (c for c in root if isinstance(c.tag, str)), None
        )
        if block is None:
            return {"error": "Template %s has no element to drop." % chosen.key}

        label = root.get("name") or name[2:].replace("_", " ").title()
        block.set("data-snippet", name)
        block.set("data-name", label)
        classes = (block.get("class") or "").split()
        if name not in classes:
            block.set("class", " ".join(classes + [name]))

        return {
            "snippet": name,
            "template": chosen.key,
            "name": label,
            "html": etree.tostring(block, encoding="unicode"),
        }
