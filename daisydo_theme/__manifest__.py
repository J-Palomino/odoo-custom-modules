{
    "name": "DaisyERP Theme",
    "version": "19.0.1.0.2",
    "post_init_hook": "post_init_hook",
    "category": "Theme",
    "summary": "Yellow daisy theme for DaisyERP",
    "description": "Replaces Odoo branding with DaisyERP yellow daisy theme.",
    "author": "DaisyERP",
    "license": "LGPL-3",
    "depends": ["web", "base", "mail_bot", "website", "auth_oauth", "daisy_bot"],
    "data": [
        "data/res_partner_data.xml",
        "views/webclient_templates.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "daisydo_theme/static/src/scss/theme.scss",
            "daisydo_theme/static/src/js/error_reporter.esm.js",
        ],
        "web.assets_frontend": [
            "daisydo_theme/static/src/scss/theme.scss",
            "daisydo_theme/static/src/scss/website.scss",
        ],
        "web._assets_primary_variables": [
            ("before", "web/static/src/scss/primary_variables.scss", "daisydo_theme/static/src/scss/primary_variables.scss"),
        ],
    },
    "installable": True,
    "auto_install": True,
}
