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
        session = self.session_class(data, sid, False)
        # Sliding expiration: refresh the TTL on every read so an actively-used
        # session never hard-expires mid-use. Without this the TTL is only set
        # on save()/rotate(), so a logged-in user whose session data doesn't
        # change counts down from login and gets logged out ~SESSION_LIFETIME
        # later regardless of activity. Don't refresh sessions mid-rotation
        # (a ``next_sid`` pointer is short-lived by design).
        if 'next_sid' not in data:
            try:
                ttl = self.expiration if session.uid else ANON_EXPIRATION
                self.redis.expire(self._key(sid), ttl)
            except Exception:
                pass
        return session

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
        """Rotate session ID — must match FilesystemSessionStore exactly.

        After rotation, computes session_token for authenticated sessions.
        Without session_token, Odoo rejects the session on the next request.
        """
        import time
        from odoo.service import security

        if soft:
            static = session.sid[:STORED_SESSION_BYTES]
            # Check if a concurrent request already rotated
            recent_session = self.get(session.sid)
            if 'next_sid' in recent_session:
                # Match stock FilesystemSessionStore exactly: adopt the sid the
                # concurrent request already persisted and let _save_session set
                # the new cookie. The next request reloads that record (which
                # already carries the correct token), so we deliberately do NOT
                # recompute the token here.
                session.sid = recent_session['next_sid']
                return

            next_sid = static + self.generate_key()[STORED_SESSION_BYTES:]
            # Save old session with pointer + short TTL
            session['next_sid'] = next_sid
            session['deletion_time'] = time.time() + SESSION_DELETION_TIMER
            self.save(session)

            # Prepare the new session
            session['gc_previous_sessions'] = True
            session.sid = next_sid
            del session['deletion_time']
            del session['next_sid']
        else:
            self.delete(session)
            session.sid = self.generate_key()

        # Compute session token for authenticated sessions
        if session.uid:
            assert env, "saving this session requires an environment"
            session.session_token = security.compute_session_token(
                session, env
            )
        session.should_rotate = False
        session['create_time'] = time.time()
        self.save(session)

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
