#!/usr/bin/env python3
"""Offline checks for the Loki log handler.

Run with plain Python — no Odoo, no database, no network:

    python3 mint_loki_logger/dev/offline_checks.py

The handler is pure stdlib, so these exercise it directly. The important one
is the real `os.fork()` test: Odoo imports this module in the master and then
forks 8 HTTP workers plus a cron worker, and threads do not survive fork().
That is not a hypothetical — it silently cost every worker's logs on
production until 2026-08-25.

Deliberately NOT in `tests/` — Odoo imports that package during its own test
runs, and forking inside the Odoo test runner is a bad idea.
"""

import logging
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("LOKI_URL", "http://loki.invalid:3100")

import loki_handler as lh  # noqa: E402

PASSED = []


def check(name):
    PASSED.append(name)
    print("  ok  %s" % name)


def make_handler():
    """A handler whose flush is captured instead of sent."""
    h = lh.LokiHandler()
    h.sent = []
    h._flush = lambda batch: h.sent.extend(batch)
    return h


def drain(h, timeout=8.0):
    """Wait for the drain thread to hand everything to _flush."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if h._queue.empty() and h.sent:
            time.sleep(h.flush_secs * 0.2)
            return h.sent
        time.sleep(0.1)
    return h.sent


def main():
    print("fork safety")
    h = make_handler()
    assert h._worker_ok(), "worker not running in parent"

    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        # ---- child ----
        os.close(read_fd)
        try:
            # The at-fork hook should already have rebuilt the worker, before
            # any emit() call gets the chance to lazily repair it.
            repaired_by_hook = h._worker_ok()
            fresh_queue = h._queue.empty()
            rec = logging.LogRecord(
                "child.logger", logging.ERROR, __file__, 10, "from child", None, None
            )
            h.emit(rec)
            got = drain(h)
            msg = "%s|%s|%d" % (repaired_by_hook, fresh_queue, len(got))
            os.write(write_fd, msg.encode())
        except Exception as exc:  # pylint: disable=broad-except
            os.write(write_fd, ("EXC:%s" % exc).encode())
        finally:
            os.close(write_fd)
            os._exit(0)

    # ---- parent ----
    os.close(write_fd)
    os.waitpid(pid, 0)
    raw = os.read(read_fd, 200).decode()
    os.close(read_fd)
    assert not raw.startswith("EXC"), "child raised: %s" % raw
    hook_ok, fresh, count = raw.split("|")
    assert hook_ok == "True", "register_at_fork did not restart the worker in the child"
    check("at-fork hook restarts the drain thread in a forked child")
    assert fresh == "True", "child reused the parent's queue"
    check("child starts a fresh queue (parent's lock/backlog not inherited)")
    assert int(count) == 1, "child shipped %s records, expected 1" % count
    check("a record emitted in the child is actually flushed")

    print("lazy repair")
    h2 = make_handler()
    h2._owner_pid = -1  # simulate a fork path that bypassed the hook
    old = h2._thread
    h2.emit(logging.LogRecord("l", logging.ERROR, __file__, 1, "x", None, None))
    assert h2._thread is not old and h2._thread.is_alive()
    check("emit() repairs a stale worker even without the hook")

    print("formatting")
    h3 = make_handler()
    args = ["before"]
    rec = logging.LogRecord("l", logging.INFO, __file__, 1, "value=%s", (args,), None)
    h3.emit(rec)
    args[0] = "after"  # mutate the argument before the drain thread runs
    sent = drain(h3)
    assert sent and "before" in sent[0][3], (
        "message was interpolated late: %r" % (sent[0][3] if sent else None)
    )
    assert "after" not in sent[0][3]
    check("records are rendered at emit() time, not on the drain thread")

    ts, level, logger, line = sent[0]
    assert ts.isdigit() and len(ts) == 19, "timestamp is not ns: %r" % ts
    assert level == "INFO" and logger == "l"
    check("queued item is a plain tuple (no LogRecord/args pinned in memory)")

    h4 = make_handler()
    try:
        raise ValueError("boom")
    except ValueError:
        rec = logging.LogRecord(
            "l", logging.ERROR, __file__, 1, "failed", None, sys.exc_info()
        )
        h4.emit(rec)
    sent = drain(h4)
    assert sent and "ValueError: boom" in sent[0][3], "traceback missing"
    check("exception tracebacks are captured")

    print("robustness")
    h5 = make_handler()

    class Hostile:
        def __str__(self):
            raise RuntimeError("unstringable")

    h5.emit(logging.LogRecord("l", logging.ERROR, __file__, 1, "%s", (Hostile(),), None))
    check("a record whose argument raises does not propagate")

    h6 = make_handler()
    h6._queue.maxsize = 1
    h6._queue.put_nowait(("0", "INFO", "l", "filler"))
    for _ in range(5):
        h6.emit(logging.LogRecord("l", logging.INFO, __file__, 1, "over", None, None))
    assert h6._dropped >= 1, "overflow was not counted"
    check("queue overflow drops and counts instead of blocking")

    h7 = make_handler()
    h7._last_failure_report = 0.0
    h7._report_failure(OSError("down"), 10)
    first = h7._last_failure_report
    h7._report_failure(OSError("down"), 10)
    assert h7._last_failure_report == first, "failure report not rate-limited"
    assert h7._dropped >= 20, "dropped count not accumulated across failures"
    check("push failures are rate-limited but keep counting drops")

    saved = os.environ.pop("LOKI_URL")
    try:
        h8 = lh.LokiHandler()
        assert h8._thread is None, "started a thread with no LOKI_URL"
        h8.emit(logging.LogRecord("l", logging.ERROR, __file__, 1, "x", None, None))
    finally:
        os.environ["LOKI_URL"] = saved
    check("no LOKI_URL is an inert no-op")

    print("\n%d checks passed" % len(PASSED))


if __name__ == "__main__":
    main()
