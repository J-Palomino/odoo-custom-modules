"""Hook for posting Odoo project.task records to the daisy.plus ci-fleet-v2
Functional Analyst agency.

The trigger fires on ``project.task.write()`` for two independent signals:

1. The ``daisy-fa`` ``project.tags`` is added to a task (absent → present).
2. A configured Daisy bot user is assigned to a task (absent → present in
   ``user_ids``). The bot user id lives in
   ``ir.config_parameter`` key ``daisy.plus.fa_user_id``.

Either signal posts the task to the configured daisy.plus agency. The
write() override enforces this universally — UI, XML-RPC, REST, server
actions, base.automation all go through write().

ir.config_parameter keys consumed:

    daisy.plus.api_key                 — Bearer token (optional)
    daisy.plus.ci_fleet_v2.agency_id   — agency UUID
    daisy.plus.fa_user_id              — res.users id of the Daisy FA bot
"""
import json
import logging
import urllib.request

from odoo import models

_logger = logging.getLogger(__name__)

_DAISY_PREDICTION_URL = 'https://daisy.plus/api/v1/prediction/{agency_id}'
_DAISY_TAG_NAME = 'daisy-fa'


class ProjectTaskDaisy(models.Model):
    _inherit = 'project.task'

    def write(self, vals):
        """Fire the daisy FA trigger on tag-add OR bot-user-assign.
        Idempotent per signal: a record that already has the tag/user won't
        re-fire on subsequent writes.
        """
        # Diagnostic — confirms the override is actually being invoked.
        _logger.warning(
            'DAISY_WRITE_HIT ids=%s keys=%s', self.ids, list(vals.keys()))

        icp = self.env['ir.config_parameter'].sudo()
        tag = self.env['project.tags'].sudo().search(
            [('name', '=', _DAISY_TAG_NAME)], limit=1)
        bot_user_id_str = icp.get_param('daisy.plus.fa_user_id') or ''
        bot_user_id = int(bot_user_id_str) if bot_user_id_str.isdigit() else 0

        had_tag_before = {
            r.id: bool(tag) and tag.id in r.tag_ids.ids for r in self}
        had_bot_before = {
            r.id: bot_user_id and bot_user_id in r.user_ids.ids
            for r in self}

        result = super().write(vals)

        tag_write = bool(tag) and 'tag_ids' in vals
        user_write = bool(bot_user_id) and 'user_ids' in vals
        if not (tag_write or user_write):
            return result

        for rec in self:
            fire = False
            reason = []
            if tag_write:
                has_tag_now = tag.id in rec.tag_ids.ids
                if has_tag_now and not had_tag_before.get(rec.id):
                    fire = True
                    reason.append('tag')
            if user_write:
                has_bot_now = bot_user_id in rec.user_ids.ids
                if has_bot_now and not had_bot_before.get(rec.id):
                    fire = True
                    reason.append('assignee')
            if fire:
                _logger.warning(
                    'DAISY_FIRE task=%s reason=%s', rec.id, ','.join(reason))
                try:
                    rec._trigger_daisy_fa()
                except Exception:
                    _logger.exception(
                        'daisy.plus FA trigger error on task %s', rec.id)
        return result

    def _trigger_daisy_fa(self):
        """POST each record in ``self`` to the configured daisy.plus agency.

        Posts a chatter message on the task summarizing success/failure.
        Never raises — failures are logged and noted on the task.
        """
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
