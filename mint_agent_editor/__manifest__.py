{
    "name": "Mint Agent Editor Panel",
    "version": "19.0.1.0.0",
    "category": "Website",
    "summary": "Chat with a Daisy agent beside the website editor and watch it edit the open page",
    "description": """
Mint Agent Editor Panel
=======================

Puts a chat panel on the same screen as the website builder. You say what to
change; the agent answers with an edit plan; the panel applies it to the page
you are looking at, live.

The agent does NOT write to the database. That is the whole point of the
design. An agent writing ``arch_db`` underneath an open editor loses a race it
cannot win: the editor holds its own copy of the DOM, and whoever saves last
wins. Either your unsaved work disappears or the agent's does.

So the agent edits the live editable DOM instead. The change is visible
immediately, it becomes part of your unsaved edit, and it is persisted by
Odoo's own Save button. Round-trip fidelity is then automatic — the editor
serialises the page, exactly as it does for a block you dragged in yourself.

Snippets come from real ``website.*`` templates, materialised server-side the
way the editor's drop does it (data-snippet + data-name stamped on), because
hand-written lookalike markup is what makes a block uneditable.
""",
    "author": "Mint Cannabis",
    "license": "LGPL-3",
    "depends": ["website", "daisydo_agents"],
    "data": [
        "views/assets.xml",
    ],
    "assets": {
        "web.assets_frontend": [
            "mint_agent_editor/static/src/scss/agent_panel.scss",
            "mint_agent_editor/static/src/js/agent_panel.js",
        ],
    },
    "installable": True,
    "application": False,
    "auto_install": False,
}
