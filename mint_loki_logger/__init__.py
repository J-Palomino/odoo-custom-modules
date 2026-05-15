from . import loki_handler

# Install the handler at module import time. Odoo imports each addon's
# package once at registry build, so this runs exactly once per process and
# attaches the handler to the root logger before any business code runs.
loki_handler.install()
