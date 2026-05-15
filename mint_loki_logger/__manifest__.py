{
    "name": "Mint Loki Logger",
    "version": "19.0.1.0.0",
    "summary": "Ship Odoo Python logs to Grafana Loki",
    "description": """
Registers a Python logging.Handler that batches Odoo log records and POSTs
them to Loki's `/loki/api/v1/push` endpoint. The handler is added to the root
logger at module import time so it captures everything Odoo emits via the
`logging` module.

Configuration via env vars on the Odoo container:
  LOKI_URL          full base URL, e.g. http://loki.railway.internal:3100
                    (no trailing slash). Required — handler is a no-op if unset.
  LOKI_LABELS_JSON  JSON dict of static labels merged into every stream.
                    Defaults to {"service":"odoo","env":"production"}.
  LOKI_USERNAME     optional basic-auth user
  LOKI_PASSWORD     optional basic-auth password
  LOKI_LEVEL        minimum log level to ship (default INFO)
  LOKI_BATCH_SIZE   max records per push (default 100)
  LOKI_FLUSH_SECS   max seconds between flushes (default 5)

Shipping is non-blocking:
  * records are queued (bounded, drops when full to avoid OOM under load)
  * a daemon thread drains the queue and POSTs batches
  * network failures are swallowed silently — never propagate to Odoo

Auto-installs because the only dep is `base` and you always want logs.
    """,
    "category": "Tools",
    "author": "Mint Cannabis",
    "website": "https://letsgomint.us",
    "license": "LGPL-3",
    "depends": ["base"],
    "installable": True,
    "application": False,
    "auto_install": True,
}
