"""Report request-scoped Odoo errors to PostHog.

`ir.http._handle_error` is the single funnel every exception raised while
serving a request passes through - both `type='http'` and `type='json'`
routes - so overriding it captures server faults the log handler cannot see,
and captures them with the context that actually makes an error actionable:
which URL, which user, which database.

It also catches failures Odoo never logs at ERROR level at all. A `UserError`
becomes a 422 and a `SessionExpiredException` becomes a redirect to the login
page; neither is logged as an error, so neither reaches a logging handler -
yet "I keep getting logged out" is exactly the complaint we are chasing.

Two constraints shape this file:

  * **No ORM.** By the time an error surfaces the cursor is frequently already
    aborted (a failed transaction raises InFailedSqlTransaction on the next
    query). Everything here is read from `request.session` and
    `request.httprequest`, which are plain in-memory objects, so reporting an
    error can never raise a second one.
  * **No request parameters.** `request.params` routinely carries passwords,
    API keys and customer PII. The path is recorded; the payload never is.
"""

import logging
import os
import threading
import time
import traceback

from werkzeug.exceptions import HTTPException

from odoo import models
from odoo.http import request

from . import posthog_server

_logger = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 2000
MAX_TRACEBACK_CHARS = 8000

# Request start times, kept per thread rather than on the request object so
# nothing is attached to core objects that might be pooled or reused.
_timing = threading.local()


def _slow_request_after_ms():
    try:
        return float(os.environ.get("MINT_POSTHOG_SLOW_REQUEST_MS", "5000"))
    except (TypeError, ValueError):
        return 5000.0

SESSION_EXPIRED = "odoo.http.SessionExpiredException"

# Deliberate business messages, not crashes. Same set the web client uses, so
# client and server events can be filtered identically.
BUSINESS_EXCEPTIONS = frozenset(
    {
        "odoo.exceptions.UserError",
        "odoo.exceptions.ValidationError",
        "odoo.exceptions.AccessError",
        "odoo.exceptions.AccessDenied",
        "odoo.exceptions.MissingError",
        "odoo.exceptions.RedirectWarning",
        "odoo.exceptions.Warning",
    }
)


def _dotted_name(exception):
    """`odoo.exceptions.UserError`, matching the name the client reports."""
    cls = type(exception)
    module = getattr(cls, "__module__", "") or ""
    return "%s.%s" % (module, cls.__name__) if module else cls.__name__


def _is_noise(exception):
    """Routing misses and redirects are not faults worth recording."""
    if isinstance(exception, HTTPException):
        code = getattr(exception, "code", None)
        if code is None or code < 400 or code in (404, 405):
            return True
    return False


class IrHttp(models.AbstractModel):
    _inherit = "ir.http"

    @classmethod
    def _handle_error(cls, exception):
        try:
            cls._mint_posthog_report(exception)
        except Exception:
            # Telemetry must never change how an error is served.
            pass
        return super()._handle_error(exception)

    # -- slow requests ------------------------------------------------------
    #
    # A request that takes twelve seconds raises nothing, logs nothing, and is
    # invisible to every error hook - but it is the most common form of "Odoo
    # is broken for me". Both overrides use *args passthrough so an upstream
    # signature change cannot break dispatch.

    @classmethod
    def _pre_dispatch(cls, *args, **kwargs):
        try:
            _timing.started = time.monotonic()
        except Exception:
            pass
        return super()._pre_dispatch(*args, **kwargs)

    @classmethod
    def _post_dispatch(cls, *args, **kwargs):
        try:
            cls._mint_posthog_report_slow()
        except Exception:
            pass
        return super()._post_dispatch(*args, **kwargs)

    @classmethod
    def _mint_posthog_report_slow(cls):
        started = getattr(_timing, "started", None)
        _timing.started = None
        if started is None or not posthog_server.is_active():
            return

        duration_ms = (time.monotonic() - started) * 1000.0
        if duration_ms < _slow_request_after_ms():
            return

        path = ""
        method = ""
        uid = None
        try:
            path = request.httprequest.path or ""
            method = request.httprequest.method or ""
            uid = request.session.uid
        except Exception:
            pass

        posthog_server.report(
            "odoo_request_slow",
            {
                "source": "request",
                "http_path": path,
                "http_method": method,
                "duration_ms": int(duration_ms),
                "odoo_uid": uid,
            },
            distinct_id="odoo-%s" % uid if uid else None,
            dedupe_key="slow:%s" % path,
        )

    @classmethod
    def _mint_posthog_report(cls, exception):
        if not posthog_server.is_active() or _is_noise(exception):
            return

        name = _dotted_name(exception)

        path = ""
        method = ""
        user_agent = ""
        try:
            http = request.httprequest
            path = http.path or ""
            method = http.method or ""
            user_agent = (http.headers.get("User-Agent") or "")[:200]
        except Exception:
            pass

        uid = None
        login = ""
        db = ""
        try:
            # Plain session data - no query, safe on an aborted cursor.
            session = request.session
            uid = session.uid
            login = session.login or ""
            db = session.db or ""
        except Exception:
            pass

        properties = {
            "source": "request",
            "exception_type": name,
            "message": str(exception)[:MAX_MESSAGE_CHARS],
            "http_path": path,
            "http_method": method,
            "http_status": getattr(exception, "code", None)
            or getattr(exception, "http_status", None),
            "user_agent": user_agent,
            "odoo_uid": uid,
            "odoo_user": login,
            "odoo_db": db,
            "is_business_error": name in BUSINESS_EXCEPTIONS,
        }

        try:
            properties["server_traceback"] = "".join(
                traceback.format_exception(
                    type(exception), exception, exception.__traceback__
                )
            )[:MAX_TRACEBACK_CHARS]
        except Exception:
            properties["server_traceback"] = ""

        # Attach to the same PostHog person as the user's browser events,
        # which identify as "odoo-<uid>".
        distinct_id = "odoo-%s" % uid if uid else None

        # Being logged out mid-task is the top user-visible failure, and it is
        # not a crash - same event name the client uses, so one metric covers
        # both sides.
        event = "odoo_session_expired" if name == SESSION_EXPIRED else "odoo_request_error"

        posthog_server.report(
            event,
            properties,
            distinct_id=distinct_id,
            dedupe_key="req:%s:%s:%s" % (event, name, path),
        )
        posthog_server.mark_reported(exception)
