"""Async, non-blocking logging.Handler that pushes records to Grafana Loki.

Reads config from env vars (see __manifest__.py for the full list). Designed
so that Loki being unreachable, slow, or misconfigured can never block Odoo:

  * records go into a bounded queue (silently drop on full)
  * a single daemon thread drains the queue
  * the thread POSTs in small batches with a short timeout
  * any push exception is swallowed and logged ONCE (to stderr, not to root
    logger — that would create a feedback loop where shipping logs to Loki
    fails and the failure tries to ship itself)
"""

import json
import logging
import os
import queue
import sys
import threading
import time
import urllib.error
import urllib.request

_INSTALLED = False
_INSTALL_LOCK = threading.Lock()


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

        self._queue: "queue.Queue[logging.LogRecord]" = queue.Queue(maxsize=10000)
        self._stop = threading.Event()
        self._reported_failure = False  # one-shot stderr report

        if self.url:
            t = threading.Thread(
                target=self._run, name="mint-loki-logger", daemon=True
            )
            t.start()

    # ── logging.Handler API ─────────────────────────────────────────
    def emit(self, record):
        if not self.url:
            return
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            # Drop on overflow rather than block. Losing logs is better than
            # blocking the request thread that's emitting them.
            pass

    # ── background drain ────────────────────────────────────────────
    def _run(self):
        batch: "list[logging.LogRecord]" = []
        last_flush = time.monotonic()
        while not self._stop.is_set():
            timeout = max(0.1, self.flush_secs - (time.monotonic() - last_flush))
            try:
                record = self._queue.get(timeout=timeout)
                batch.append(record)
            except queue.Empty:
                pass

            should_flush = (
                len(batch) >= self.batch_size
                or (batch and (time.monotonic() - last_flush) >= self.flush_secs)
            )
            if should_flush:
                self._flush(batch)
                batch = []
                last_flush = time.monotonic()

    def _flush(self, batch):
        if not batch:
            return
        # Group by labels: streams in Loki are keyed by label set, so we
        # bucket per (level, logger) to give useful filters in Grafana.
        streams: "dict[tuple, list]" = {}
        for record in batch:
            labels = dict(self.static_labels)
            labels["level"] = record.levelname
            # Truncate logger name so labels don't blow up Loki's index
            labels["logger"] = (record.name or "root")[:80]
            key = tuple(sorted(labels.items()))
            ts_ns = str(int(record.created * 1_000_000_000))
            line = self.format(record)
            streams.setdefault(key, []).append([ts_ns, line])

        payload = {
            "streams": [
                {"stream": dict(k), "values": v} for k, v in streams.items()
            ]
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
                    f"{self.username}:{self.password}".encode()
                ).decode()
                req.add_header("Authorization", f"Basic {token}")
            with urllib.request.urlopen(req, timeout=5):
                pass
        except (urllib.error.URLError, OSError, ValueError) as exc:
            if not self._reported_failure:
                self._reported_failure = True
                # Bypass the Python logger to avoid feedback loop
                sys.stderr.write(
                    f"[mint_loki_logger] push failed (suppressing further reports): {exc}\n"
                )

    def format(self, record):
        # Compact one-line format with file:line and function. Loki dedupes
        # by stream+timestamp+line, so include enough context that two records
        # in the same ms aren't collapsed.
        try:
            msg = record.getMessage()
        except Exception:  # pylint: disable=broad-except
            msg = record.msg
        loc = f"{record.filename}:{record.lineno}:{record.funcName}"
        out = f"{loc} | {msg}"
        if record.exc_info:
            out += "\n" + logging.Handler.format(self, record).split("\n", 1)[-1]
        return out


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
            f"[mint_loki_logger] installed, shipping to {handler.url} "
            f"(level={handler.level}, batch={handler.batch_size}, flush={handler.flush_secs}s)\n"
        )
