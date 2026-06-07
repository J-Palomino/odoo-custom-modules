"""Hook for dispatching Odoo project.task records to daisy.plus agencies.

Two layered triggers fire from ``project.task`` create/write:

1. **Legacy Functional Analyst** — the ``daisy-fa`` ``project.tags`` is added,
   OR the configured FA bot user (``ir.config_parameter`` key
   ``daisy.plus.fa_user_id``) is assigned. Posts the task to the ci-fleet-v2
   dispatcher agency (``daisy.plus.ci_fleet_v2.agency_id``).

2. **Role agents (data-driven)** — any newly-assigned user that is linked to a
   ``daisy.agent`` record carrying a ``daisy_agency_id`` fires *that agent's*
   own daisy.plus agency, passing ``task_id`` so the role agency (e.g.
   daisy-qa / daisy-developer / daisy-network-infra) reads the ticket and
   posts its own work to the task chatter. Adding a new assignable agent is
   therefore zero-code: create a ``daisy.agent`` linked to a ``res.users``
   with a ``daisy_agency_id``.

Both create() and write() are overridden so the trigger is universal — UI,
XML-RPC, REST, server actions, base.automation, AND inbound mail aliases
(which create() tasks with an assignee via ``alias_defaults``).

ir.config_parameter keys consumed:

    daisy.plus.api_key                 — Bearer token for ci-fleet-v2 (optional)
    daisy.plus.ci_fleet_v2.agency_id   — FA dispatcher agency UUID
    daisy.plus.fa_user_id              — res.users id of the Daisy FA bot
    daisy.api_base_url                 — daisy.plus API base (optional)
    daisy_bot.api_key                  — global Bearer fallback for role agents
"""
import json
import logging
import socket
import urllib.request

from odoo import api, models

_logger = logging.getLogger(__name__)

_DAISY_PREDICTION_URL = 'https://daisy.plus/api/v1/prediction/{agency_id}'
_DAISY_API_BASE_DEFAULT = 'https://daisy.plus/api/v1'
_DAISY_TAG_NAME = 'daisy-fa'


class ProjectTaskDaisy(models.Model):
    _inherit = 'project.task'

    # ------------------------------------------------------------------ hooks
    @api.model_create_multi
    def create(self, vals_list):
        """Dispatch on create so mail-alias / API-created tasks that arrive
        pre-assigned to a daisy agent also trigger. Never blocks create."""
        records = super().create(vals_list)
        try:
            tag = self.env['project.tags'].sudo().search(
                [('name', '=', _DAISY_TAG_NAME)], limit=1)
            for rec in records:
                user_ids = set(rec.user_ids.ids)
                tag_added = bool(tag) and tag.id in rec.tag_ids.ids
                if user_ids or tag_added:
                    rec._daisy_dispatch(user_ids, tag_added=tag_added)
        except Exception:
            _logger.exception('daisy.plus create-dispatch error')
        return records

    def write(self, vals):
        """Fire daisy triggers on tag-add OR newly-assigned agent user.
        Idempotent: a user/tag already present won't re-fire."""
        # Diagnostic — confirms the override is actually being invoked.
        _logger.warning(
            'DAISY_WRITE_HIT ids=%s keys=%s', self.ids, list(vals.keys()))

        tag = self.env['project.tags'].sudo().search(
            [('name', '=', _DAISY_TAG_NAME)], limit=1)
        track_tag = bool(tag) and 'tag_ids' in vals
        track_users = 'user_ids' in vals

        had_tag_before = {
            r.id: bool(tag) and tag.id in r.tag_ids.ids
            for r in self} if track_tag else {}
        users_before = {
            r.id: set(r.user_ids.ids) for r in self} if track_users else {}

        result = super().write(vals)

        if not (track_tag or track_users):
            return result

        for rec in self:
            tag_added = (
                track_tag and tag.id in rec.tag_ids.ids
                and not had_tag_before.get(rec.id))
            added_users = (
                set(rec.user_ids.ids) - users_before.get(rec.id, set())
                if track_users else set())
            if tag_added or added_users:
                rec._daisy_dispatch(added_users, tag_added=tag_added)
        return result

    # ------------------------------------------------------------- internals
    def _daisy_fa_user_id(self):
        s = self.env['ir.config_parameter'].sudo().get_param(
            'daisy.plus.fa_user_id') or ''
        return int(s) if s.isdigit() else 0

    def _daisy_dispatch(self, added_user_ids, tag_added=False):
        """Route one task: legacy FA (tag/FA-user) + any role agents whose
        linked user was just assigned. Each branch is best-effort."""
        self.ensure_one()
        fa_uid = self._daisy_fa_user_id()

        # 1) Legacy Functional Analyst (tag-add or FA bot assigned)
        if tag_added or (fa_uid and fa_uid in added_user_ids):
            reason = 'tag' if tag_added else 'assignee'
            _logger.warning('DAISY_FIRE task=%s reason=%s', self.id, reason)
            try:
                self._trigger_daisy_fa()
            except Exception:
                _logger.exception(
                    'daisy.plus FA trigger error on task %s', self.id)

        # 2) Data-driven role agents — any newly-assigned user mapped to a
        #    daisy.agent with its own agency (FA user excluded; handled above).
        other = {u for u in added_user_ids if u != fa_uid}
        if not other:
            return
        agents = self.env['daisy.agent'].sudo().search([
            ('user_id', 'in', list(other)),
            ('daisy_agency_id', '!=', False),
            ('active', '=', True),
        ])
        for agent in agents:
            _logger.warning(
                'DAISY_AGENT_FIRE task=%s agent=%s agency=%s',
                self.id, agent.code, agent.daisy_agency_id)
            try:
                self._trigger_daisy_agent(agent)
            except Exception:
                _logger.exception(
                    'daisy.plus agent dispatch error task=%s agent=%s',
                    self.id, agent.code)

    def _trigger_daisy_fa(self):
        """POST each record in ``self`` to the ci-fleet-v2 dispatcher agency.
        Posts a chatter note summarizing success/failure. Never raises."""
        icp = self.env['ir.config_parameter'].sudo()
        agency_id = icp.get_param('daisy.plus.ci_fleet_v2.agency_id')
        api_key = icp.get_param('daisy.plus.api_key')
        if not agency_id:
            _logger.warning('daisy.plus.ci_fleet_v2.agency_id is not configured')
            return

        url = _DAISY_PREDICTION_URL.format(agency_id=agency_id)
        headers = {'Content-Type': 'application/json'}
        if api_key:
            headers['Authorization'] = f'Bearer {api_key}'

        for rec in self:
            payload = json.dumps({
                'question': (
                    f'Odoo project.task id={rec.id} title={rec.name!r} — '
                    f'task_source=odoo_task. Read the record and post the '
                    f'Functional Analysis to chatter.'
                ),
            }).encode('utf-8')
            req = urllib.request.Request(url, data=payload, headers=headers, method='POST')
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    status = resp.status
                rec.message_post(
                    body=f'Daisy FA trigger sent (HTTP {status}). Agency: {agency_id}',
                    subject='Daisy FA trigger',
                    message_type='comment',
                )
            except Exception as exc:
                _logger.exception('daisy.plus FA trigger failed for task %s', rec.id)
                rec.message_post(
                    body=f'Daisy FA trigger FAILED: {exc!r}',
                    subject='Daisy FA trigger error',
                    message_type='comment',
                )

    def _trigger_daisy_agent(self, agent):
        """POST this task to the daisy.plus agency bound to ``agent``.

        Passes ``task_id`` so the role agency reads the ticket and posts its
        own work to chatter. Role agencies (e.g. Playwright QA) often run
        longer than the client timeout — a timeout is treated as "delivered,
        running" since the agent posts its result independently. Never raises.
        """
        self.ensure_one()
        icp = self.env['ir.config_parameter'].sudo()
        agency_id = agent.daisy_agency_id
        if not agency_id:
            return
        api_key = (
            agent.daisy_api_key
            or icp.get_param('daisy.plus.api_key')
            or icp.get_param('daisy_bot.api_key'))
        base = (icp.get_param('daisy.api_base_url')
                or _DAISY_API_BASE_DEFAULT).rstrip('/')
        url = f'{base}/prediction/{agency_id}'
        headers = {'Content-Type': 'application/json'}
        if api_key:
            headers['Authorization'] = f'Bearer {api_key}'

        question = (
            f'task_id={self.id}. Odoo project.task id={self.id} '
            f'title={self.name!r}. You are {agent.name} ({agent.role}). '
            f'task_source=odoo_task. Read the ticket and post your work to '
            f'the task chatter.'
        )
        payload = json.dumps({'question': question}).encode('utf-8')
        req = urllib.request.Request(url, data=payload, headers=headers, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                status = resp.status
            self.message_post(
                body=(f'Dispatched to {agent.name} ({agent.role}) — '
                      f'agency {agency_id} (HTTP {status}).'),
                subject=f'Daisy agent: {agent.name}',
                message_type='comment',
            )
        except (socket.timeout, TimeoutError):
            self.message_post(
                body=(f'Dispatched to {agent.name} ({agent.role}) — running; '
                      f'its response will post to chatter when ready.'),
                subject=f'Daisy agent: {agent.name}',
                message_type='comment',
            )
        except Exception as exc:
            _logger.exception(
                'daisy.plus agent dispatch failed task=%s agent=%s',
                self.id, agent.code)
            self.message_post(
                body=f'Daisy agent {agent.name} dispatch FAILED: {exc!r}',
                subject='Daisy agent error',
                message_type='comment',
            )

    # ---------------------------------------------------- SDLC pipeline (#94837)
    # Autonomous SDLC loop, Phase 1. On entry to a pipeline stage in project 120
    # ("Agent Workflow"), enqueue that stage's daisy.agent. Dispatch goes through
    # the daisy.agent.job QUEUE (daisy.agent._enqueue_response), NOT user_ids
    # assignment, to avoid the synchronous urllib-in-write SerializationFailure.
    _SDLC_STAGE_AGENT = {
        944: 'devid',   # Build  → Web Dev
        945: 'eriq',    # QA     → QA Tester
        946: 'nic',     # Deploy → Network & Infrastructure Coordinator
    }

    def _sdlc_enqueue_for_stage(self):
        """Queue the daisy.agent mapped to each task's current stage.

        Invoked by the per-stage ``base.automation`` server action on
        ``on_stage_set``. Enqueues a ``daisy.agent.job`` so the agency runs
        async (cron-processed) and posts its result to the task chatter.
        Never raises — logs and skips on any problem.
        """
        Agent = self.env['daisy.agent'].sudo()
        for rec in self:
            code = self._SDLC_STAGE_AGENT.get(rec.stage_id.id)
            if not code:
                continue
            agent = Agent.search(
                [('code', '=', code), ('active', '=', True)], limit=1)
            if not agent or not agent.daisy_agency_id:
                _logger.warning(
                    'SDLC: no usable daisy.agent %r for task %s stage %s',
                    code, rec.id, rec.stage_id.id)
                continue
            prompt = (
                f'task_id={rec.id}. You are handling the "{rec.stage_id.name}" '
                f'stage of Odoo project.task id={rec.id} (title={rec.name!r}). '
                f'Read the ticket and post your work to the task chatter.'
            )
            try:
                agent._enqueue_response(
                    'project.task', rec.id, prompt, [], False,
                    session_id=f'task-{rec.id}')
                _logger.info(
                    'SDLC_ENQUEUE task=%s stage=%s agent=%s',
                    rec.id, rec.stage_id.id, code)
            except Exception:
                _logger.exception(
                    'SDLC enqueue failed task=%s agent=%s', rec.id, code)
