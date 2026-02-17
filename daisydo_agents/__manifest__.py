{
    "name": "Daisy Agency",
    "version": "19.0.1.0.0",
    "category": "Productivity",
    "summary": "AI agents as first-class Odoo users with livechat auto-response",
    "author": "DaisyERP",
    "license": "LGPL-3",
    "depends": ["daisydo_livechat", "im_livechat", "mail", "project"],
    "data": [
        "security/agent_security.xml",
        "security/ir.model.access.csv",
        "wizard/agent_import_views.xml",
        "views/daisy_agent_views.xml",
        "views/daisy_agent_menus.xml",
        "data/daisy_agent_data.xml",
    ],
    "auto_install": False,
}
