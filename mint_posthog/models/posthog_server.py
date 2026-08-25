"""Forward Odoo server-side errors to PostHog.

The JS half of this module only sees what the browser sees. Anything that
fails without a browser attached - cron jobs, webhooks, controller 500s,
queue workers - has until now existed only in the Railway stdout log, which
is not searchable, not alertable, and rolls over.

This attaches a logging handler to the root logger that ships ERROR and
CRITICAL records to PostHog as `odoo_server_error` events, so server faults
sit next to the client-side ones and can be trended and alerted on.

Safety, because this runs in-process on every worker of a production ERP:

  * OFF unless MINT_POSTHOG_SERVER_CAPTURE is set. Configuration comes from
    the environment, never the database, so the logging path never touches a
    cursor and can never deadlock behind the ORM.
  * emit() only ever does a dict build and a non-blocking queue put. All
    network I/O happens on one daemon thread.
  * The queue is bounded. When it is full, records are dropped and counted,
    and the running drop count rides along on the next event that gets
    through - a flood shows up as data rather than as backpressure.
  * The handler mutes itself and the HTTP stack it uses, and holds a
    thread-local re-entry guard, so a failure while reporting a failure can
    never recurse.
  * Every path is wrapped. A logging handler must never raise.
"""

import json
import logging
import os
import queue
import threading
import time
import urllib.request

_logger = logging.getLogger(__name__)

DEFAULT_HOST = "https://us.i.posthog.com"
# Same "LetsGoMint" project (544449) the web client reports into.
DEFAULT_KEY = "phc_pn6qyCiqURG5TQodSwspowqCYQiKGz92tD3N2GeCjRBT"

MAX_QUEUE = 1000
BATCH_MAX = 50
FLUSH_INTERVAL = 5.0
HTTP_TIMEOUT = 5
MAX_MESSAGE_CHARS = 2000
MAX_TRACEBACK_CHARS = 8000
# Identical (logger, location) failures inside this window collapse into one.
DEDUPE_WINDOW = 30.0

# Set on an exception object once ir.http._handle_error has reported it, so the
# log handler does not report the same failure a second time with less context.
REPORTED_FLAG = "_mint_posthog_reported"

# Loggers that would either recurse through this handler or add nothing.
MUTED_LOGGERS = (
    "mint_posthog",
    "urllib3",
    "requests",
    "werkzeug",
    "odoo.sql_db",  # already re-raised and logged by the caller with context
)

_state = threading.local()


def _enabled():
    return os.environ.get("MINT_POSTHOG_SERVER_CAPTURE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


class PostHogLogHandler(logging.Handler):
    """Ship ERROR+ log records to PostHog off the request thread."""

    def __init__(self, api_key, host):
        super().__init__(level=logging.ERROR)
        self._api_key = api_key
        self._endpoint = host.rstrip("/") + "/batch/"
        self._queue = queue.Queue(maxsize=MAX_QUEUE)
        self._dropped = 0
        self._dropped_lock = threading.Lock()
        self._last_seen = {}
        self._worker = threading.Thread(
            target=self._run, name="mint_posthog.sender", daemon=True
        )
        self._worker.start()

    # -- producer side (runs on the thread that logged) ---------------------

    def emit(self, record):
        if getattr(_state, "busy", False):
            return
        try:
            _state.busy = True
            payload = self._build(record)
            if payload is None:
                return
            self.enqueue(payload)
        except Exception:
            # A logging handler must never raise into application code.
            pass
        finally:
            _state.busy = False

    def enqueue(self, payload):
        """Hand a built event to the sender thread. Never blocks, never raises."""
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            self.note_dropped()

    def note_dropped(self):
        with self._dropped_lock:
            self._dropped += 1

    def seen_recently(self, key):
        """True if `key` was already reported inside the dedupe window."""
        now = time.time()
        last = self._last_seen.get(key)
        if last is not None and now - last < DEDUPE_WINDOW:
            self.note_dropped()
            return True
        if len(self._last_seen) > 500:
            self._last_seen.clear()
        self._last_seen[key] = now
        return False

    def _build(self, record):
        name = record.name or ""
        if name.startswith(MUTED_LOGGERS):
            return None

        # Request-scoped failures are reported by ir.http._handle_error, which
        # has far better context (path, method, user, company). It marks the
        # exception object so the same failure is not counted twice here.
        if record.exc_info and len(record.exc_info) > 1:
            try:
                if getattr(record.exc_info[1], REPORTED_FLAG, False):
                    return None
            except Exception:
                pass

        if self.seen_recently("%s:%s:%s" % (name, record.pathname, record.lineno)):
            return None

        try:
            message = record.getMessage()
        except Exception:
            message = str(getattr(record, "msg", ""))

        traceback_text = ""
        exception_type = ""
        if record.exc_info:
            try:
                exc_type = record.exc_info[0]
                exception_type = exc_type.__name__ if exc_type else ""
                traceback_text = logging.Formatter().formatException(record.exc_info)
            except Exception:
                traceback_text = ""

        properties = {
            "app": "odoo-server",
            "level": record.levelname,
            "logger": name,
            "message": message[:MAX_MESSAGE_CHARS],
            "exception_type": exception_type,
            "server_traceback": traceback_text[:MAX_TRACEBACK_CHARS],
            "pathname": record.pathname,
            "lineno": record.lineno,
            "func": record.funcName,
            "process": record.process,
            "thread_name": record.threadName,
            "dyno": os.environ.get("RAILWAY_SERVICE_NAME", ""),
            "deployment": os.environ.get("RAILWAY_DEPLOYMENT_ID", ""),
        }

        # Odoo stamps the active db/uid onto the log record via its own filter
        # when it is available; include it when present so a server error can
        # be tied to a user the same way a client one can.
        for attr, prop in (("dbname", "odoo_db"), ("uid", "odoo_uid")):
            value = getattr(record, attr, None)
            if value not in (None, ""):
                properties[prop] = value

        return {
            "event": "odoo_server_error",
            "distinct_id": "odoo-server-%s" % (properties.get("dyno") or "unknown"),
            "properties": properties,
            "timestamp": time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)
            )
            + "Z",
        }

    # -- consumer side (the single daemon thread) ---------------------------

    def _run(self):
        batch = []
        deadline = time.time() + FLUSH_INTERVAL
        while True:
            try:
                timeout = max(0.1, deadline - time.time())
                try:
                    batch.append(self._queue.get(timeout=timeout))
                except queue.Empty:
                    pass
                if batch and (len(batch) >= BATCH_MAX or time.time() >= deadline):
                    self._send(batch)
                    batch = []
                    deadline = time.time() + FLUSH_INTERVAL
            except Exception:
                # Never let the sender thread die - that would silently end
                # all server-side capture until the worker restarts.
                batch = []
                deadline = time.time() + FLUSH_INTERVAL
                time.sleep(FLUSH_INTERVAL)

    def _send(self, batch):
        with self._dropped_lock:
            dropped, self._dropped = self._dropped, 0
        if dropped:
            batch[-1]["properties"]["suppressed_since_last"] = dropped
        body = json.dumps({"api_key": self._api_key, "batch": batch}).encode("utf-8")
        request = urllib.request.Request(
            self._endpoint,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT):
                pass
        except Exception:
            # Telemetry is best-effort. Re-queueing on failure risks unbounded
            # growth during an outage, so the batch is dropped.
            with self._dropped_lock:
                self._dropped += len(batch)


_handler = None


def install():
    """Attach the handler once per worker process. Called from post_load."""
    global _handler
    if _handler is not None:
        return
    if not _enabled():
        _logger.info(
            "mint_posthog: server-side error capture disabled "
            "(set MINT_POSTHOG_SERVER_CAPTURE=1 to enable)"
        )
        return
    try:
        _handler = PostHogLogHandler(
            os.environ.get("MINT_POSTHOG_KEY", DEFAULT_KEY),
            os.environ.get("MINT_POSTHOG_HOST", DEFAULT_HOST),
        )
        logging.getLogger().addHandler(_handler)
        _logger.info("mint_posthog: server-side error capture active")
    except Exception:
        _logger.exception("mint_posthog: could not install server error capture")
        _handler = None


def is_active():
    """True when server-side capture is installed and running."""
    return _handler is not None


def report(event, properties, distinct_id=None, dedupe_key=None):
    """Queue an arbitrary server-side event.

    This is the seam `ir.http` uses to report request errors. It is a no-op
    unless capture is enabled, and it never raises - callers are already on an
    error path and must not be given a second failure to handle.
    """
    handler = _handler
    if handler is None:
        return
    try:
        if dedupe_key and handler.seen_recently(dedupe_key):
            return
        properties.setdefault("app", "odoo-server")
        properties.setdefault("dyno", os.environ.get("RAILWAY_SERVICE_NAME", ""))
        properties.setdefault(
            "deployment", os.environ.get("RAILWAY_DEPLOYMENT_ID", "")
        )
        handler.enqueue(
            {
                "event": event,
                # Matches the id the web client uses ("odoo-" + uid), so a
                # user's server errors land on the same PostHog person as
                # their browser errors.
                "distinct_id": distinct_id
                or ("odoo-server-%s" % (properties.get("dyno") or "unknown")),
                "properties": properties,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
            }
        )
    except Exception:
        pass


def mark_reported(exception):
    """Flag an exception so the log handler will not report it again."""
    try:
        setattr(exception, REPORTED_FLAG, True)
    except Exception:
        # Some exception types use __slots__ and reject attributes. Failing
        # here only risks a duplicate event, which is the safe direction.
        pass
