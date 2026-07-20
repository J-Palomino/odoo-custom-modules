"""Dispatch Eriq (daisy.agent) for Engineering maintenance-request triage.

Two independent triggers, both funneling into the same generic daisy.agent.job
queue proven on project.task (project_task_daisy._sdlc_enqueue_for_stage) and
already exercised ad hoc against maintenance.request (job #17561, Synthia,
state=done) — no new infra, just the dispatch wiring:

  1. Swimlane move into "In Progress" (stage_id) — driven by a base.automation
     (data/eng_triage_automations.xml), mirroring the proven SDLC pattern
     exactly (on_stage_set + a dedicated ir.actions.server; #94837 taught us a
     shared server action silently fails to link — each automation gets its
     own).
  2. kanban_state flips to "blocked" — handled here via a write() override
     instead of a base.automation field-domain rule. on_create_or_write with
     filter_pre_domain/filter_domain would need trigger_field_ids pointed at
     kanban_state's ir.model.fields record; that field's owning module (and
     therefore its external id) wasn't confirmed live (Odoo was down when
     checked), so a write() diff is the robust choice — it needs no field
     xmlid and can't silently drift if a future module changes who owns the
     field.

Both paths are idempotent for free: daisy.agent._enqueue_response collapses a
duplicate enqueue (same agent+target+text+session, still pending) into one,
so kanban_state flapping blocked->normal->blocked can't spam Eriq.
"""
import logging

from odoo import api, models

_logger = logging.getLogger(__name__)

ENGINEERING_TEAM_ID = 4     # maintenance.team "Engineering" (verified live)
IN_PROGRESS_STAGE_ID = 2    # maintenance.stage "In Progress" on that team
ERIQ_AGENT_CODE = 'eriq'    # daisy.agent id 45, verified: code='eriq', daisy_agency_id set


class MaintenanceRequestDaisy(models.Model):
    _inherit = 'maintenance.request'

    def write(self, vals):
        # Snapshot pre-write state for the records where kanban_state is
        # changing, so we can detect entry into 'blocked' after the super()
        # call commits the new values.
        newly_blocked = self.browse()
        if vals.get('kanban_state') == 'blocked':
            newly_blocked = self.filtered(
                lambda r: r.maintenance_team_id.id == ENGINEERING_TEAM_ID
                and r.kanban_state != 'blocked'
            )
        res = super().write(vals)
        if newly_blocked:
            newly_blocked._eng_dispatch_triage('flagged Blocked')
        return res

    def _eng_dispatch_triage_on_stage(self):
        """Called by the base.automation server action on entry to
        "In Progress" (data/eng_triage_automations.xml). Filters to
        Engineering here too, defense-in-depth against the automation's
        filter_domain (mirrors the SDLC precedent's belt-and-suspenders
        style)."""
        self.filtered(
            lambda r: r.maintenance_team_id.id == ENGINEERING_TEAM_ID
        )._eng_dispatch_triage('entered In Progress')

    def _eng_dispatch_triage(self, reason):
        """Enqueue Eriq to triage each record in self. Never raises — logs
        and skips on any problem, same contract as _sdlc_enqueue_for_stage."""
        Agent = self.env['daisy.agent'].sudo()
        agent = Agent.search(
            [('code', '=', ERIQ_AGENT_CODE), ('active', '=', True)], limit=1)
        if not agent or not agent.daisy_agency_id:
            _logger.warning(
                'Eng triage: no usable daisy.agent %r for maintenance.request %s',
                ERIQ_AGENT_CODE, self.ids)
            return
        for rec in self:
            prompt = (
                f'maintenance_request_id={rec.id}. You are triaging Engineering '
                f'maintenance ticket #{rec.id} (just {reason}): '
                f'subject={rec.name!r}. Read the ticket description and chatter, '
                f'investigate the issue, and post your triage findings and a '
                f'suggested next step to the ticket chatter.'
            )
            try:
                # The queue worker posts as agent.user_id via message_post —
                # requires the agent to already be a follower (same fix as
                # QA #94837's "security restrictions" job error).
                if agent.partner_id:
                    rec.message_subscribe(partner_ids=agent.partner_id.ids)
                agent._enqueue_response(
                    'maintenance.request', rec.id, prompt, [], False,
                    session_id=f'mr-{rec.id}')
                _logger.info(
                    'ENG_TRIAGE_ENQUEUE mr=%s reason=%r agent=%s',
                    rec.id, reason, ERIQ_AGENT_CODE)
            except Exception:
                _logger.exception(
                    'Eng triage enqueue failed mr=%s reason=%r', rec.id, reason)
