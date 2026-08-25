#!/usr/bin/env python3
"""Offline checks for the server-side capture paths.

Run with plain Python, no Odoo and no database required:

    python3 mint_posthog/dev/offline_checks.py

Odoo is stubbed, so these verify the parts that are ours: the safety
properties that make it acceptable to run this in-process on a production ERP
(never raises, never recurses, never blocks, never leaks request parameters)
and the contracts with core (every override returns super()'s result
unchanged, every failure still propagates).

Deliberately NOT in `tests/` - Odoo imports that package during its own test
runs, where these stubs would collide with the real thing.
"""

import logging
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _install_stubs():
    class AbstractModel:
        @classmethod
        def _handle_error(cls, exception):
            return "SUPER_ERR"

        @classmethod
        def _pre_dispatch(cls, *a, **k):
            return "SUPER_PRE"

        @classmethod
        def _post_dispatch(cls, *a, **k):
            return "SUPER_POST"

    class Model:
        id = 42

        def __len__(self):
            return 1

        def _callback(self, *a, **k):
            if getattr(self, "_boom", None):
                raise self._boom
            return "CRON_OK"

    models_mod = types.ModuleType("odoo.models")
    models_mod.AbstractModel = AbstractModel
    models_mod.Model = Model
    odoo = types.ModuleType("odoo")
    odoo.models = models_mod

    class Session:
        uid = 7
        login = "staff@example.com"
        db = "odoo"

    class HttpRequest:
        path = "/web/dataset/call_kw"
        method = "POST"
        headers = {"User-Agent": "UA"}

    class Request:
        httprequest = HttpRequest()
        session = Session()
        # Must never reach PostHog.
        params = {"password": "SUPERSECRET", "login": "staff@example.com"}

    http_mod = types.ModuleType("odoo.http")
    http_mod.request = Request()

    wz = types.ModuleType("werkzeug")
    wze = types.ModuleType("werkzeug.exceptions")

    class HTTPException(Exception):
        code = None

    class NotFound(HTTPException):
        code = 404

    wze.HTTPException = HTTPException
    wze.NotFound = NotFound
    wz.exceptions = wze

    sys.modules.update(
        {
            "odoo": odoo,
            "odoo.models": models_mod,
            "odoo.http": http_mod,
            "werkzeug": wz,
            "werkzeug.exceptions": wze,
        }
    )
    return NotFound


PASSED = []


def check(name):
    PASSED.append(name)
    print("  ok  %s" % name)


def main():
    os.environ["MINT_POSTHOG_SERVER_CAPTURE"] = "1"
    os.environ["RAILWAY_SERVICE_NAME"] = "offline-checks"
    os.environ["MINT_POSTHOG_SLOW_CRON_SECONDS"] = "300"
    os.environ["MINT_POSTHOG_SLOW_REQUEST_MS"] = "999999"
    sys.path.insert(0, ROOT)
    NotFound = _install_stubs()

    from mint_posthog.models import ir_cron, ir_http
    from mint_posthog.models import posthog_server as ps

    handler = ps.PostHogLogHandler(ps.DEFAULT_KEY, ps.DEFAULT_HOST)
    ps._handler = handler
    # Nothing is sent anywhere: events are read straight off the queue.
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)

    def drain():
        out = []
        while not handler._queue.empty():
            out.append(handler._queue.get_nowait())
        return out

    class UserError(Exception):
        pass

    UserError.__module__ = "odoo.exceptions"

    class SessionExpiredException(Exception):
        pass

    SessionExpiredException.__module__ = "odoo.http"

    print("request errors (ir.http._handle_error)")
    assert ir_http._dotted_name(UserError("x")) == "odoo.exceptions.UserError"
    assert ir_http._dotted_name(UserError("x")) in ir_http.BUSINESS_EXCEPTIONS
    check("exception names match the web client's vocabulary")

    assert ir_http.IrHttp._handle_error(UserError("boom")) == "SUPER_ERR"
    events = drain()
    assert len(events) == 1, events
    props = events[0]["properties"]
    assert props["is_business_error"] is True
    assert props["http_path"] == "/web/dataset/call_kw"
    assert props["odoo_uid"] == 7 and events[0]["distinct_id"] == "odoo-7"
    assert props["server_traceback"]
    check("returns super() unchanged, captures context, joins to odoo-7")

    assert "SUPERSECRET" not in repr(events[0])
    check("request params and passwords never leave the process")

    assert ir_http.IrHttp._handle_error(NotFound()) == "SUPER_ERR"
    assert drain() == []
    check("404s filtered as noise")

    ir_http.IrHttp._handle_error(SessionExpiredException("expired"))
    assert drain()[0]["event"] == "odoo_session_expired"
    check("session expiry reuses the client's event name")

    original_report = ps.report
    ps.report = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("telemetry down"))
    assert ir_http.IrHttp._handle_error(UserError("still served")) == "SUPER_ERR"
    ps.report = original_report
    check("broken telemetry never changes how an error is served")

    print("slow requests (ir.http._pre/_post_dispatch)")
    assert ir_http.IrHttp._pre_dispatch("rule", {}) == "SUPER_PRE"
    assert ir_http.IrHttp._post_dispatch("resp") == "SUPER_POST"
    assert drain() == []
    check("fast request stays silent, super() preserved")

    os.environ["MINT_POSTHOG_SLOW_REQUEST_MS"] = "0"
    ir_http.IrHttp._pre_dispatch("rule", {})
    ir_http.IrHttp._post_dispatch("resp")
    events = drain()
    assert events[0]["event"] == "odoo_request_slow"
    assert events[0]["distinct_id"] == "odoo-7"
    check("slow request captured and joined to the person")

    ir_http.IrHttp._post_dispatch("resp")
    assert drain() == []
    check("_post_dispatch without _pre_dispatch is a no-op")

    print("scheduled actions (ir.cron._callback)")
    cron = ir_cron.IrCron()
    assert cron._callback("PTL lifecycle", 11) == "CRON_OK"
    events = drain()
    assert events[0]["event"] == "odoo_cron_run"
    assert events[0]["properties"]["cron_name"] == "PTL lifecycle"
    assert events[0]["properties"]["succeeded"] is True
    check("successful run emits a heartbeat")

    cron._callback("PTL lifecycle", 11)
    assert drain() == []
    check("heartbeat deduped to one per cron per hour")

    boom = RuntimeError("cron exploded")
    cron._boom = boom
    raised = False
    try:
        cron._callback("Discount sync", 12)
    except RuntimeError:
        raised = True
    assert raised, "cron failure must still propagate"
    events = drain()
    assert events[0]["event"] == "odoo_cron_failed"
    assert events[0]["properties"]["server_traceback"]
    assert getattr(boom, ps.REPORTED_FLAG, False) is True
    check("failure propagates, is captured, and is marked against double-count")
    cron._boom = None

    os.environ["MINT_POSTHOG_SLOW_CRON_SECONDS"] = "0"
    cron._callback("Slow job", 13)
    assert drain()[0]["event"] == "odoo_cron_slow"
    os.environ["MINT_POSTHOG_SLOW_CRON_SECONDS"] = "300"
    check("overrunning cron captured")

    print("log handler")
    assert handler.level == logging.INFO, handler.level
    logging.getLogger("odoo.addons.base.models.res_users").info(
        "Login failed for login:%s from %s", "staff@example.com", "1.2.3.4"
    )
    events = drain()
    assert len(events) == 1 and events[0]["event"] == "odoo_server_log"
    assert "Login failed" in events[0]["properties"]["message"]
    check("failed logins captured at INFO, kept out of error metrics")

    logging.getLogger("odoo.addons.something.else").info("chatty")
    assert drain() == []
    check("non-allowlisted INFO loggers ignored")

    logging.getLogger("werkzeug").error("per-request noise")
    assert drain() == []
    check("muted loggers stay muted even at ERROR")

    log = logging.getLogger("odoo.offline.checks")
    try:
        raise ValueError("already reported by a hook")
    except ValueError as exc:
        ps.mark_reported(exc)
        log.exception("should be skipped")
    assert drain() == []
    check("a hook-reported exception is not counted twice")

    try:
        raise KeyError("never reported")
    except KeyError:
        log.exception("should be captured")
    assert len(drain()) == 1
    check("an unmarked exception still reports (guard fails safe)")

    class Slotted(Exception):
        __slots__ = ()

    ps.mark_reported(Slotted("no __dict__"))
    check("__slots__ exceptions cannot break marking")

    class Hostile:
        def __str__(self):
            raise RuntimeError("unstringable")

    log.error("hostile %s", Hostile())
    drain()
    check("a record whose argument raises does not propagate")

    ps.report("x", {"bad": object()}, dedupe_key="junk")
    check("report() never raises on unserializable input")

    print("\n%d checks passed" % len(PASSED))


if __name__ == "__main__":
    main()
