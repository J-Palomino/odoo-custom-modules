from odoo import api, models


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    # mint_is_employee is stored on res.users and computed cross-company via
    # sudo, so the ORM's dependency graph cannot see these changes on its own.
    # Every path that can create, move or remove the user link retriggers it.

    @api.model_create_multi
    def create(self, vals_list):
        employees = super().create(vals_list)
        employees.mapped('user_id')._mint_mark_is_employee_for_recompute()
        return employees

    def write(self, vals):
        # Capture the users we are about to detach from, so a reassignment
        # clears the flag on the old user as well as setting it on the new one.
        users_before = self.mapped('user_id')
        res = super().write(vals)
        if 'user_id' in vals or 'active' in vals:
            (users_before | self.mapped('user_id'))._mint_mark_is_employee_for_recompute()
        return res

    def unlink(self):
        users = self.mapped('user_id')
        res = super().unlink()
        users.exists()._mint_mark_is_employee_for_recompute()
        return res
