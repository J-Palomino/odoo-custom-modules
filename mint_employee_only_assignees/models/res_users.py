from odoo import api, fields, models


class ResUsers(models.Model):
    _inherit = 'res.users'

    mint_is_employee = fields.Boolean(
        string='Is Employee',
        compute='_compute_mint_is_employee',
        store=True,
        index=True,
        compute_sudo=True,
        help="True when this user is linked to an hr.employee record in ANY company.\n\n"
             "Core alternatives cannot drive an assignee domain:\n"
             "  * res.users.employee_ids is scoped to the active company, so on this "
             "multi-company database it misses employees of the per-store companies.\n"
             "  * res.users.employee is not stored, so it cannot be searched.",
    )

    @api.depends('employee_ids')
    def _compute_mint_is_employee(self):
        linked = self._mint_linked_employee_user_ids(self.ids)
        for user in self:
            user.mint_is_employee = user.id in linked

    @api.model
    def _mint_linked_employee_user_ids(self, user_ids):
        """Return the subset of ``user_ids`` that have an hr.employee anywhere.

        Deliberately cross-company and archive-inclusive: employees live in
        per-store companies, and an archived employee record still means the
        user is staff rather than a portal customer or a bot.
        """
        user_ids = [uid for uid in (user_ids or []) if uid]
        if not user_ids:
            return set()
        employees = self.env['hr.employee'].sudo().with_context(
            active_test=False,
            allowed_company_ids=self.env['res.company'].sudo().search([]).ids,
        ).search_read([('user_id', 'in', user_ids)], ['user_id'])
        return {e['user_id'][0] for e in employees if e.get('user_id')}

    def _mint_mark_is_employee_for_recompute(self):
        """Queue ``mint_is_employee`` for recompute on these users.

        Used by the hr.employee hooks and the post-init backfill. The ORM does
        not see cross-company employee changes through the @api.depends chain,
        so the trigger is explicit.
        """
        if self:
            self.env.add_to_compute(
                self._fields['mint_is_employee'],
                self.sudo().with_context(active_test=False),
            )
