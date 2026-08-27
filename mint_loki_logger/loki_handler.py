"""Async, non-blocking logging.Handler that pushes records to Grafana Loki.

Reads config from env vars (see __manifest__.py for the full list). Designed
so that Loki being unreachable, slow, or misconfigured can never block Odoo:

  * records are formatted on the calling thread, then queued (bounded, drops
    on full)
  * a per-process daemon thread drains the queue
  * the thread POSTs in small batches with a short timeout
  * push exceptions are swallowed and reported to stderr at most once a
    minute (never through the root logger — that would be a feedback loop
    where failing to ship a log tries to ship the failure)

---------------------------------------------------------------------------
FORK SAFETY - the reason this file exists in its current shape.

Odoo's PreforkServer calls `preload_registries()` in the MASTER process and
only then `process_spawn()`s its workers, so every addon's Python - including
this one - is imported *before* the fork. **Threads do not survive fork().**
A child inherits this handler object, its queue, and the root-logger
attachment, but NOT the thread draining that queue.

Without the guard below, that meant: the master shipped its own sparse logs
while all 8 HTTP workers plus the cron worker filled a 10k queue exactly once
and then dropped every subsequent record, silently, for the life of the
process. Measured on production 2026-08-25: Loki held only `odoo.registry`,
`odoo.service.server` and `odoo.sql_db` - master-process loggers - and not one
`werkzeug` or `odoo.addons.*` line, while Odoo emitted ~13 lines/second from
its workers.

The child also must NOT reuse the inherited queue. `queue.Queue` is built on
`threading.Lock`; if the parent's drain thread happened to hold that lock at
the moment of fork, the child inherits it locked with no thread alive to
release it, and the first `put_nowait()` would block forever - turning a
telemetry bug into an Odoo hang. So the child always starts a fresh queue.
---------------------------------------------------------------------------
"""

import json
import logging
import os
import queue
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import weakref

_INSTALLED = False
_INSTALL_LOCK = threading.Lock()

MAX_QUEUE = 10000
MAX_LINE_CHARS = 16000
# Report a sustained push failure at most this often, per process.
FAILURE_REPORT_SECS = 60.0

# Every live handler, so the at-fork hook can repair all of them. Weak so a
# discarded handler is not kept alive by this registry.
_HANDLERS = weakref.WeakSet()


def _env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _parse_labels():
    raw = _env("LOKI_LABELS_JSON")
    if not raw:
        return {"service": "odoo", "env": "production"}
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return {"service": "odoo", "env": "production"}
        return {str(k): str(v) for k, v in parsed.items()}
    except (ValueError, TypeError):
        return {"service": "odoo", "env": "production"}


def _reinit_after_fork():
    """Restart every handler's drain thread inside a freshly forked child."""
    for handler in list(_HANDLERS):
        try:
            handler._restart_worker()
        except Exception:  # pylint: disable=broad-except
            pass


if hasattr(os, "register_at_fork"):
    # Runs in the child immediately after fork, while it is still
    # single-threaded, which is the only safe moment to rebuild the queue.
    os.register_at_fork(after_in_child=_reinit_after_fork)


class LokiHandler(logging.Handler):
    """Batches log records and POSTs them to a Loki push endpoint."""

    def __init__(self):
        super().__init__()
        self.url = _env("LOKI_URL", "").rstrip("/")
        self.username = _env("LOKI_USERNAME")
        self.password = _env("LOKI_PASSWORD")
        self.static_labels = _parse_labels()
        try:
            self.batch_size = max(1, int(_env("LOKI_BATCH_SIZE", "100")))
        except ValueError:
            self.batch_size = 100
        try:
            self.flush_secs = max(1.0, float(_env("LOKI_FLUSH_SECS", "5")))
        except ValueError:
            self.flush_secs = 5.0

        self._stop = threading.Event()
        self._last_failure_report = 0.0
        self._dropped = 0
        self._owner_pid = None
        self._queue = queue.Queue(maxsize=MAX_QUEUE)
        self._thread = None

        if self.url:
            _HANDLERS.add(self)
            self._restart_worker()

    # ── fork handling ───────────────────────────────────────────────
    def _restart_worker(self):
        """(Re)create the queue and drain thread for the current process.

        Safe to call from the at-fork hook (child is single-threaded) and
        from emit() (serialised by logging.Handler's own lock).
        """
        self._owner_pid = os.getpid()
        # Never reuse the parent's queue: its internal lock may have been
        # held by the parent's drain thread at fork time, and its contents
        # are the parent's unshipped backlog, not ours.
        self._queue = queue.Queue(maxsize=MAX_QUEUE)
        self._dropped = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="mint-loki-logger", daemon=True
        )
        self._thread.start()

    def _worker_ok(self):
        return (
            self._owner_pid == os.getpid()
            and self._thread is not None
            and self._thread.is_alive()
        )

    # ── logging.Handler API ─────────────────────────────────────────
    def emit(self, record):
        if not self.url:
            return
        try:
            # Belt-and-braces for any fork path that bypasses the hook, and
            # for a drain thread that died unexpectedly.
            if not self._worker_ok():
                self._restart_worker()
            item = self._prepare(record)
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                # Drop on overflow rather than block. Losing logs is better
                # than blocking the request thread that's emitting them.
                self._dropped += 1
        except Exception:  # pylint: disable=broad-except
            # A logging handler must never raise into application code.
            pass

    def _prepare(self, record):
        """Render to a plain tuple on the calling thread.

        Formatting here rather than on the drain thread matters twice over:
        `record.getMessage()` interpolates `msg % args` lazily, so a mutable
        argument mutated before the flush would be rendered wrong; and holding
        LogRecords keeps their args alive, which on Odoo means pinning whole
        recordsets for up to flush_secs x 10k records inside a worker capped
        at 2 GB.
        """
        try:
            msg = record.getMessage()
        except Exception:  # pylint: disable=broad-except
            msg = str(getattr(record, "msg", ""))
        line = "%s:%s:%s | %s" % (
            record.filename,
            record.lineno,
            record.funcName,
            msg,
        )
        if record.exc_info:
            try:
                line += "\n" + "".join(traceback.format_exception(*record.exc_info))
            except Exception:  # pylint: disable=broad-except
                pass
        return (
            str(int(record.created * 1_000_000_000)),
            record.levelname,
            (record.name or "root")[:80],
            line[:MAX_LINE_CHARS],
        )

    # ── background drain ────────────────────────────────────────────
    def _run(self):
        batch = []
        last_flush = time.monotonic()
        while not self._stop.is_set():
            timeout = max(0.1, self.flush_secs - (time.monotonic() - last_flush))
            try:
                batch.append(self._queue.get(timeout=timeout))
            except queue.Empty:
                pass
            except Exception:  # pylint: disable=broad-except
                pass

            should_flush = len(batch) >= self.batch_size or (
                batch and (time.monotonic() - last_flush) >= self.flush_secs
            )
            if should_flush:
                try:
                    self._flush(batch)
                except Exception:  # pylint: disable=broad-except
                    # The drain thread must never die; that would silently end
                    # shipping for this process until it restarts.
                    pass
                batch = []
                last_flush = time.monotonic()

    def _flush(self, batch):
        if not batch:
            return
        # Streams in Loki are keyed by label set, so bucket per (level,
        # logger) to give useful filters in Grafana. Measured cardinality on
        # this instance is ~14 loggers, so this stays far away from Loki's
        # high-cardinality failure mode.
        streams = {}
        for ts_ns, levelname, logger_name, line in batch:
            labels = dict(self.static_labels)
            labels["level"] = levelname
            labels["logger"] = logger_name
            streams.setdefault(tuple(sorted(labels.items())), []).append([ts_ns, line])

        payload = {
            "streams": [{"stream": dict(k), "values": v} for k, v in streams.items()]
        }
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                self.url + "/loki/api/v1/push",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            if self.username and self.password:
                import base64

                token = base64.b64encode(
                    ("%s:%s" % (self.username, self.password)).encode()
                ).decode()
                req.add_header("Authorization", "Basic %s" % token)
            with urllib.request.urlopen(req, timeout=5):
                pass
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self._report_failure(exc, len(batch))

    def _report_failure(self, exc, lost):
        """Rate-limited stderr report.

        The previous one-shot flag meant a Loki outage produced exactly one
        line ever, so a permanently broken pipeline looked identical to a
        healthy one for the rest of the process's life.
        """
        self._dropped += lost
        now = time.monotonic()
        if now - self._last_failure_report < FAILURE_REPORT_SECS:
            return
        self._last_failure_report = now
        try:
            sys.stderr.write(
                "[mint_loki_logger] pid=%s push failed (%s); %d records dropped "
                "since start, next report in >=%ds\n"
                % (os.getpid(), exc, self._dropped, int(FAILURE_REPORT_SECS))
            )
        except Exception:  # pylint: disable=broad-except
            pass


def install():
    """Idempotently attach the handler to the root logger."""
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return
        url = _env("LOKI_URL")
        if not url:
            # Silently no-op when not configured. Useful for local dev.
            _INSTALLED = True
            return
        handler = LokiHandler()
        try:
            level_name = _env("LOKI_LEVEL", "INFO").upper()
            handler.setLevel(getattr(logging, level_name, logging.INFO))
        except (AttributeError, TypeError):
            handler.setLevel(logging.INFO)
        logging.getLogger().addHandler(handler)
        _INSTALLED = True
        sys.stderr.write(
            "[mint_loki_logger] installed pid=%s, shipping to %s "
            "(level=%s, batch=%s, flush=%ss)\n"
            % (
                os.getpid(),
                handler.url,
                handler.level,
                handler.batch_size,
                handler.flush_secs,
            )
        )
