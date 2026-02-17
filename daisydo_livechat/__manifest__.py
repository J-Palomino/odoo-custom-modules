{
    "name": "Daisy+ Chat",
    "version": "19.0.1.0.0",
    "category": "Website/Live Chat",
    "summary": "AI-powered live chat widget for DaisyERP",
    "description": """
        Daisy+ Chat - Intelligent Live Chat for DaisyERP
        ================================================

        Features:
        - Custom branded chat widget (embeddable)
        - AI-powered auto-responses
        - Smart routing and escalation
        - External API integration
        - Analytics and insights
    """,
    "author": "DaisyERP",
    "license": "LGPL-3",
    "depends": ["im_livechat", "website", "mail"],
    "data": [
        "security/ir.model.access.csv",
        "data/daisy_chat_data.xml",
        "views/daisy_chat_views.xml",
        "views/daisy_chat_templates.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "daisydo_livechat/static/src/scss/daisy_chat_backend.scss",
        ],
        "web.assets_frontend": [
            "daisydo_livechat/static/src/scss/daisy_chat_widget.scss",
            "daisydo_livechat/static/src/js/daisy_chat_widget.js",
        ],
        "im_livechat.assets_embed_core": [
            "daisydo_livechat/static/src/scss/daisy_chat_widget.scss",
            "daisydo_livechat/static/src/js/daisy_chat_widget.js",
        ],
    },
    "auto_install": True,
    "post_init_hook": "post_init_hook",
}
