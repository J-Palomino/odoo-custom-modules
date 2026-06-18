from odoo import api, fields, models


class MintSocialScheduleRequest(models.Model):
    """Agent-friendly single-call scheduler reachable over the CRUD-only MCP.

    Creating one record (account, when, content, platforms, ...) runs
    project.task.social_schedule() and stores the outcome on the record, so an
    agent (Sage) can schedule a post with a single create and read back the
    result. Runs with the caller's permissions — schedulers submit, only
    approvers send.
    """

    _name = "mint.social.schedule.request"
    _description = "Social Post Schedule Request (agent entry point)"
    _order = "id desc"

    account = fields.Char(required=True, help="Brand/account name, e.g. 'Mint Florida'.")
    when = fields.Char(required=True, help="Date/time, e.g. '2026-12-20 09:00' or 'Dec 20 9am'.")
    content = fields.Text(required=True, help="The post caption/content.")
    platforms = fields.Char(help="Comma-separated, e.g. 'instagram'. Defaults to the account's connected platforms.")
    title = fields.Char(help="Optional card title; defaults to the content.")
    image_url = fields.Char(help="Optional public image URL to attach as the asset.")

    result_ok = fields.Boolean(readonly=True)
    result_state = fields.Char(readonly=True)
    result_message = fields.Text(readonly=True)
    result_task_id = fields.Many2one("project.task", readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            res = self.env["project.task"].social_schedule(
                account=rec.account,
                when=rec.when,
                content=rec.content,
                platforms=rec.platforms or None,
                title=rec.title or None,
                image_url=rec.image_url or None,
            )
            rec.write(
                {
                    "result_ok": bool(res.get("ok")),
                    "result_state": res.get("state") or ("error" if not res.get("ok") else False),
                    "result_message": res.get("message") or res.get("error") or res.get("needs")
                    or res.get("warning"),
                    "result_task_id": res.get("task_id"),
                }
            )
        return records
