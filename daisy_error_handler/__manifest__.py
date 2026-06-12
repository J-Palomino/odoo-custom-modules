{
    "name": "Daisy Error Handler",
    "version": "19.0.3.0.0",
    "category": "Technical",
    "summary": "Non-blocking Daisy+ AI analysis (toast) for unexpected errors",
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
