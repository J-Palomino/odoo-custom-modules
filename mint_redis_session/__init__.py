import logging
import os

_logger = logging.getLogger(__name__)


def _setup_redis_session_store():
    """Replace the default FilesystemSessionStore with RedisSessionStore."""
    redis_url = os.environ.get('REDIS_SESSION_URL', '')

    if not redis_url:
        _logger.info(
            "mint_redis_session: No REDIS_SESSION_URL set, "
            "keeping default filesystem sessions"
        )
        return

    try:
        import redis as redis_lib  # noqa: F811
    except ImportError:
        _logger.error(
            "mint_redis_session: 'redis' package not installed — "
            "pip install redis"
        )
        return

    from . import session as redis_session

    try:
        store = redis_session.setup_redis_store(redis_lib, redis_url)
        if store:
            import odoo.http
            # Override the cached_property — instance __dict__ takes priority
            # over the class descriptor, so this permanently replaces the store
            odoo.http.root.__dict__['session_store'] = store
            _logger.info(
                "mint_redis_session: Redis session store activated (%s)",
                store.prefix,
            )
        else:
            _logger.warning(
                "mint_redis_session: setup returned None, "
                "keeping filesystem sessions"
            )
    except Exception:
        _logger.exception(
            "mint_redis_session: Failed to initialize, "
            "falling back to filesystem sessions"
        )


_setup_redis_session_store()
