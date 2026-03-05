{
    "name": "Daisy Error Handler",
    "version": "19.0.2.0.0",
    "category": "Technical",
    "summary": "Route all errors through Daisy+ AI for user-friendly responses",
    "depends": ["web"],
    "assets": {
        "web.assets_backend": [
            "daisy_error_handler/static/src/xml/error_dialog.xml",
            "daisy_error_handler/static/src/js/error_handler.js",
        ],
    },
    "data": [
        "data/ir_config_parameter.xml",
    ],
    "license": "LGPL-3",
    "installable": True,
    "auto_install": False,
}
