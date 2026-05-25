"""Hook for posting Odoo project.task records to the daisy.plus ci-fleet-v2
Functional Analyst agency.

When the ``daisy-fa`` ``project.tags`` is added to a task — via ANY write
path (UI, XML-RPC, REST, base.automation, server actions) — this module's
``write()`` override fires :py:meth:`_trigger_daisy_fa` against the newly
tagged record. A ``base.automation`` rule is still acceptable but no
longer required, because :py:meth:`write` enforces the trigger universally.

API key + agency ID live in ``ir.config_parameter``:

    daisy.plus.api_key
    daisy.plus.ci_fleet_v2.agency_id
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
        """Fire the daisy FA trigger when the ``daisy-fa`` tag transitions
        from absent → present on a record. Idempotent: a record that already
        has the tag will not re-fire on subsequent writes.
        """
        # Identify the tag id once (cheap; cached for the transaction).
        tag = self.env['project.tags'].sudo().search(
            [('name', '=', _DAISY_TAG_NAME)], limit=1)
        had_tag_before = {r.id: tag and tag.id in r.tag_ids.ids for r in self}
        result = super().write(vals)
        if tag and 'tag_ids' in vals:
            for rec in self:
                has_tag_now = tag.id in rec.tag_ids.ids
                if has_tag_now and not had_tag_before.get(rec.id):
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
        # The prediction endpoint does not require auth (UUID-as-secret model);
        # we still send the key if configured, in case daisy enables it later.
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
