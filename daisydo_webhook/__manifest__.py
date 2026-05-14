{
    "name": "DaisyDo Webhooks",
    "version": "19.0.1.0.1",
    "post_init_hook": "post_init_hook",
    "category": "Technical",
    "summary": "Send webhook notifications on record changes via FastAPI",
    "description": """
        DaisyDo Webhooks
        ================

        Fires HTTP events to the FastAPI bridge whenever records are
        created, updated, or deleted in Odoo. FastAPI then fans the
        events out to registered subscriber URLs with HMAC signing
        and retry logic.

        Configuration via System Parameters:
        - daisydo_webhook.enabled — "0" or "1"
        - daisydo_webhook.fastapi_url — e.g. http://fastapi:8000
        - daisydo_webhook.event_secret — shared secret
        - daisydo_webhook.models — "*" or JSON list of model names
    """,
    "author": "DaisyERP",
    "license": "LGPL-3",
    "depends": ["base", "mail"],
    "data": [
        "data/webhook_config.xml",
    ],
    "auto_install": True,
}
