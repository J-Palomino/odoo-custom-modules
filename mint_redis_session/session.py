"""Redis-backed session store for Odoo 19.

Implements the same interface as Odoo's FilesystemSessionStore so it can
be used as a drop-in replacement.  Sessions are stored as JSON in Redis
with a TTL matching Odoo's SESSION_LIFETIME (7 days by default).
"""

import json
import logging
import re
import secrets

from odoo.http import (
    SESSION_LIFETIME,
    SESSION_DELETION_TIMER,
    STORED_SESSION_BYTES,
    Session,
)
from odoo.tools._vendor.sessions import SessionStore

_logger = logging.getLogger(__name__)

# Odoo 19 session IDs are 84-char base64url-safe strings
_SID_RE = re.compile(r'^[A-Za-z0-9_-]{84}$')

# Anonymous sessions expire faster (3 hours)
ANON_EXPIRATION = 3 * 60 * 60


class RedisSessionStore(SessionStore):
    """Store Werkzeug/Odoo sessions in Redis."""

    def __init__(self, redis_client, prefix='odoo:session',
                 expiration=SESSION_LIFETIME):
        super().__init__(session_class=Session)
        self.redis = redis_client
        self.prefix = prefix
        self.expiration = expiration
        # Provide a path attribute — some code accesses it
        self.path = '/tmp/odoo-sessions-redis'

    # ── Key helpers ──────────────────────────────────────────────

    def _key(self, sid):
        return f'{self.prefix}:{sid}'

    # ── SessionStore interface ───────────────────────────────────

    def is_valid_key(self, key):
        return bool(key and _SID_RE.match(key))

    def generate_key(self, salt=None):
        """Generate an 84-char base64url session ID matching Odoo 19 format."""
        # 63 random bytes → 84 base64url chars (no padding)
        return secrets.token_urlsafe(63)[:84]

    def get(self, sid):
        """Load session from Redis. Returns new session if not found."""
        if not self.is_valid_key(sid):
            return self.new()
        try:
            raw = self.redis.get(self._key(sid))
        except Exception:
            _logger.warning("Redis GET failed for session %s…", sid[:8],
                            exc_info=True)
            return self.new()
        if raw is None:
            return self.new()
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            _logger.warning("Corrupt session data for %s…", sid[:8])
            return self.new()
        return self.session_class(data, sid, False)

    def save(self, session):
        """Persist session to Redis with appropriate TTL."""
        try:
            data = json.dumps(dict(session))
            ttl = self.expiration if session.uid else ANON_EXPIRATION
            self.redis.setex(self._key(session.sid), ttl, data)
        except Exception:
            _logger.warning("Redis SETEX failed for session %s…",
                            session.sid[:8], exc_info=True)

    def save_if_modified(self, session):
        """Save only if the session was modified."""
        if session.should_save:
            self.save(session)

    def delete(self, session):
        """Remove session from Redis."""
        try:
            self.redis.delete(self._key(session.sid))
        except Exception:
            _logger.warning("Redis DELETE failed for session %s…",
                            session.sid[:8], exc_info=True)

    def rotate(self, session, env, soft=False):
        """Rotate session ID (hard or soft).

        Soft rotation keeps the first 42 chars (identifier) stable and
        only changes the last 42.  A pointer from the old SID is saved
        briefly so concurrent requests can follow the redirect.
        """
        if soft:
            old_sid = session.sid
            # Keep identifier (first 42 chars), regenerate second half
            new_tail = secrets.token_urlsafe(
                STORED_SESSION_BYTES
            )[:STORED_SESSION_BYTES]
            new_sid = old_sid[:STORED_SESSION_BYTES] + new_tail

            # Save redirect pointer on old SID with short TTL
            try:
                redirect_data = json.dumps({
                    **dict(session),
                    '_next_sid': new_sid,
                })
                self.redis.setex(
                    self._key(old_sid),
                    SESSION_DELETION_TIMER,
                    redirect_data,
                )
            except Exception:
                _logger.warning("Redis soft-rotate pointer failed",
                                exc_info=True)

            session.sid = new_sid
            self.save(session)
        else:
            # Hard rotation — completely new SID
            old_sid = session.sid
            session.sid = self.generate_key()
            self.save(session)
            try:
                self.redis.delete(self._key(old_sid))
            except Exception:
                pass

    def delete_old_sessions(self, session):
        """Clean up redirect pointers after soft rotation.

        No-op: Redis TTL on the old pointer handles cleanup automatically.
        """

    def vacuum(self, max_lifetime=SESSION_LIFETIME):
        """Garbage collect expired sessions.

        No-op: Redis TTL handles expiry automatically.
        Called by ir.http._gc_sessions autovacuum.
        """
        return None

    def get_missing_session_identifiers(self, identifiers):
        """Check which session identifiers (first 42 chars) have no sessions."""
        missing = set()
        for ident in identifiers:
            try:
                pattern = f'{self.prefix}:{ident}*'
                cursor, keys = self.redis.scan(0, match=pattern, count=100)
                if not keys:
                    missing.add(ident)
            except Exception:
                pass
        return missing

    def delete_from_identifiers(self, identifiers):
        """Delete all sessions matching the given identifier prefixes."""
        for ident in identifiers:
            try:
                cursor = 0
                pattern = f'{self.prefix}:{ident}*'
                while True:
                    cursor, keys = self.redis.scan(
                        cursor, match=pattern, count=100
                    )
                    if keys:
                        self.redis.delete(*keys)
                    if cursor == 0:
                        break
            except Exception:
                _logger.warning("Failed to delete sessions for ident %s…",
                                ident[:8], exc_info=True)


def setup_redis_store(redis_lib, redis_url):
    """Create and return a RedisSessionStore, or None on failure."""
    try:
        client = redis_lib.Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
            retry_on_timeout=True,
        )
        # Verify connectivity
        client.ping()
        _logger.info(
            "Redis session store connected: %s",
            redis_url.split('@')[-1],
        )
        return RedisSessionStore(redis_client=client)
    except Exception:
        _logger.exception(
            "Cannot connect to Redis at %s",
            redis_url.split('@')[-1],
        )
        return None
