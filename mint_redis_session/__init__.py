import logging
import os

_logger = logging.getLogger(__name__)


def _setup_redis_session_store():
    """Replace the default FilesystemSessionStore with RedisSessionStore.

    This runs at import time (server_wide_modules), which is BEFORE
    odoo.http.root is created.  We therefore patch the Application CLASS
    so that whenever ``root.session_store`` is first accessed, it returns
    our Redis store instead of the default filesystem one.
    """
    redis_url = os.environ.get('REDIS_SESSION_URL', '')

    if not redis_url:
        _logger.info(
            "mint_redis_session: No REDIS_SESSION_URL set, "
            "keeping default filesystem sessions"
        )
        return

    try:
        import redis as redis_lib
    except ImportError:
        _logger.error(
            "mint_redis_session: 'redis' package not installed — "
            "pip install redis"
        )
        return

    from . import session as redis_session

    store = redis_session.setup_redis_store(redis_lib, redis_url)
    if not store:
        _logger.warning(
            "mint_redis_session: setup returned None, "
            "keeping filesystem sessions"
        )
        return

    # Patch the Application class so the Redis store is used regardless
    # of when odoo.http.root is created (it's None at this point).
    import odoo.http
    Application = odoo.http.Application

    # Replace the cached_property with a simple property that always
    # returns our Redis store.  This ensures every worker (including
    # those forked later) uses Redis.
    Application.session_store = property(lambda self: store)
    _logger.info(
        "mint_redis_session: Patched Application.session_store → Redis (%s)",
        store.prefix,
    )


_setup_redis_session_store()
