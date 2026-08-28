# PostHog analytics - JS for the web client, a log handler for the server.
from . import models


def post_load():
    """Attach the server-side error handler once per worker process.

    post_load runs before the registry exists, which is exactly what we want:
    configuration is read from the environment, so the logging path never
    needs a cursor.
    """
    models.posthog_server.install()
