# -*- coding: utf-8 -*-
"""Schedule-driven online presence for Daisy agents.

Daisy agents are bots that never open a browser session, so Odoo never creates
a `mail.presence` heartbeat for them and they always show as "offline" in
Discuss. This module makes an agent's `im_status` reflect its own availability:

  * force_online           -> always "online"
  * state == 'active' AND within the agent's resource.calendar hours -> "online"
  * otherwise -> "offline"

No heartbeat cron is needed because `res.partner.im_status` is a (non-stored)
computed field — we simply override the compute for partners linked to an
active agent.
"""
import logging

import pytz

from odoo import fields, models

_logger = logging.getLogger(__name__)


class DaisyAgent(models.Model):
    _inherit = "daisy.agent"

    resource_calendar_id = fields.Many2one(
        "resource.calendar",
        string="Availability Schedule",
        help="Working hours during which this agent appears online in Discuss. "
        "Leave empty to appear online whenever the agent is Active.",
    )
    force_online = fields.Boolean(
        string="Force Online",
        default=False,
        help="Always appear online in Discuss, ignoring the availability "
        "schedule (the agent must still be Active).",
    )

    def _is_available_now(self):
        """Return True if this agent should display as online right now."""
        self.ensure_one()
        if self.state != "active":
            return False
        if self.force_online:
            return True
        cal = self.resource_calendar_id
        if not cal:
            # No schedule configured: online whenever Active.
            return True
        tz = pytz.timezone(cal.tz or "UTC")
        now = fields.Datetime.now().replace(tzinfo=pytz.utc).astimezone(tz)
        hour = now.hour + now.minute / 60.0
        dow = str(now.weekday())  # '0' = Monday .. '6' = Sunday
        # NOTE: two-week alternating calendars (week_type) are treated as a
        # single combined schedule — good enough for agent availability.
        for att in cal.attendance_ids:
            if att.dayofweek == dow and att.hour_from <= hour <= att.hour_to:
                return True
        return False


class ResPartner(models.Model):
    _inherit = "res.partner"

    def _compute_im_status(self):
        super()._compute_im_status()
        if not self.ids:
            return
        agents = self.env["daisy.agent"].sudo().search(
            [("partner_id", "in", self.ids), ("state", "=", "active")]
        )
        if not agents:
            return
        by_partner = {a.partner_id.id: a for a in agents}
        for partner in self:
            agent = by_partner.get(partner.id)
            if agent:
                partner.im_status = "online" if agent._is_available_now() else "offline"
