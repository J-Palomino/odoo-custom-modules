from odoo import fields, models

# Only staff, and only live accounts. Kept as one constant so every assignee
# dropdown in the system answers to a single definition.
EMPLOYEE_ASSIGNEE_DOMAIN = "[('mint_is_employee', '=', True), ('active', '=', True)]"


class MailActivity(models.Model):
    _inherit = 'mail.activity'

    # The everyday "assign this to someone" box (Schedule Activity / To-Do).
    # Core ships this with an empty domain, so all 1,146 users -- including 831
    # portal customers -- were selectable.
    user_id = fields.Many2one(domain=EMPLOYEE_ASSIGNEE_DOMAIN)


class ProjectTask(models.Model):
    _inherit = 'project.task'

    # Core already excluded portal users here; this narrows it further to staff.
    user_ids = fields.Many2many(domain=EMPLOYEE_ASSIGNEE_DOMAIN)


class ProjectProject(models.Model):
    _inherit = 'project.project'

    user_id = fields.Many2one(domain=EMPLOYEE_ASSIGNEE_DOMAIN)


class MaintenanceRequest(models.Model):
    _inherit = 'maintenance.request'

    user_id = fields.Many2one(domain=EMPLOYEE_ASSIGNEE_DOMAIN)
