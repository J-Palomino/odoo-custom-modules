"""Report scheduled-action outcomes to PostHog.

A failing cron is the quietest kind of failure in Odoo: nobody is watching, no
user sees an error, and the traceback lands in a rolling stdout log. Worse is
a cron that stops running at all - a disabled or unscheduled job produces no
traceback to find, so nothing surfaces it until someone notices the work was
never done. That failure mode has cost this instance months of silence before.

So three things are reported:

  * `odoo_cron_failed` - the job raised. Carries the cron name and the
    traceback.
  * `odoo_cron_slow`   - the job succeeded but took too long, which is how a
    job that is about to start overlapping itself announces itself.
  * `odoo_cron_run`    - a heartbeat, at most one per cron per hour. This is
    the one that makes absence detectable: alert on a cron whose heartbeat
    stops, rather than waiting for a traceback that will never come.

`_callback` is wrapped with `*args, **kwargs` passthrough rather than by
restating Odoo's signature. Every cron in the system runs through this method,
so an argument-list change upstream must not be able to break them all.
"""

import logging
import os
import time
import traceback

from odoo import models

from . import posthog_server

_logger = logging.getLogger(__name__)

MAX_TRACEBACK_CHARS = 8000
HEARTBEAT_WINDOW = 3600.0


def _slow_after():
    try:
        return float(os.environ.get("MINT_POSTHOG_SLOW_CRON_SECONDS", "300"))
    except (TypeError, ValueError):
        return 300.0


class IrCron(models.Model):
    _inherit = "ir.cron"

    def _callback(self, *args, **kwargs):
        started = time.monotonic()
        cron_name = ""
        try:
            cron_name = (args[0] if args else kwargs.get("cron_name", "")) or ""
        except Exception:
            pass

        try:
            result = super()._callback(*args, **kwargs)
        except Exception as exception:
            self._mint_posthog_report(cron_name, started, exception)
            raise
        self._mint_posthog_report(cron_name, started, None)
        return result

    def _mint_posthog_report(self, cron_name, started, exception):
        """Never raises: a broken report must not break the scheduler."""
        try:
            if not posthog_server.is_active():
                return

            duration = time.monotonic() - started
            properties = {
                "source": "cron",
                "cron_name": cron_name,
                # self.id is already in memory; reading self.name would issue a
                # query, and on the failure path the transaction was just
                # rolled back. cron_name carries the same value for free.
                "cron_id": self.id if len(self) == 1 else None,
                "duration_ms": int(duration * 1000),
                "succeeded": exception is None,
            }

            if exception is not None:
                cls = type(exception)
                properties["exception_type"] = "%s.%s" % (
                    getattr(cls, "__module__", "") or "",
                    cls.__name__,
                )
                properties["message"] = str(exception)[:2000]
                try:
                    properties["server_traceback"] = "".join(
                        traceback.format_exception(
                            cls, exception, exception.__traceback__
                        )
                    )[:MAX_TRACEBACK_CHARS]
                except Exception:
                    properties["server_traceback"] = ""

                posthog_server.report(
                    "odoo_cron_failed",
                    properties,
                    dedupe_key="cron_failed:%s" % cron_name,
                )
                # The scheduler logs this at ERROR too; don't count it twice.
                posthog_server.mark_reported(exception)
                return

            if duration >= _slow_after():
                posthog_server.report(
                    "odoo_cron_slow", properties, dedupe_key="cron_slow:%s" % cron_name
                )
                return

            posthog_server.report(
                "odoo_cron_run",
                properties,
                dedupe_key="cron_run:%s" % cron_name,
                dedupe_window=HEARTBEAT_WINDOW,
            )
        except Exception:
            pass
