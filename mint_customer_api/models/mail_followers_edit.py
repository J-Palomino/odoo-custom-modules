# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class MailFollowersEdit(models.TransientModel):
    """Keep customers out of the chatter's Add Followers dialog.

    Odoo 19 routes "Add Followers" through this wizard (it replaced the old
    ``mail.wizard.invite``). Two things happen here:

    * ``guard_customers`` tells the form view whether the document being
      edited is an internal-only one, so the partner picker can filter
      customers out. The user never sees them in the dropdown.
    * ``edit_followers`` re-checks server-side and raises. The view domain is
      a convenience, not a control — a stale form or a crafted RPC can still
      submit a customer id, and this is the interactive path where a silent
      drop would look like the button simply did nothing.

    The silent-drop counterpart for non-interactive callers lives in
    ``mail_thread.py``.
    """

    _inherit = 'mail.followers.edit'

    guard_customers = fields.Boolean(compute='_compute_guard_customers')
    # The view binds the picker's domain to this field rather than computing
    # one inline, so the rule lives in Python and the guarded-model list stays
    # config-driven instead of hard-coded in XML.
    partner_domain = fields.Char(compute='_compute_guard_customers')

    @api.depends('res_model')
    def _compute_guard_customers(self):
        guarded = self.env['mail.thread']._mint_follower_guard_models()
        for wizard in self:
            wizard.guard_customers = wizard.res_model in guarded
            wizard.partner_domain = (
                "[('is_customer_contact', '=', False)]"
                if wizard.guard_customers else "[]"
            )

    def edit_followers(self):
        for wizard in self:
            # 'remove' must stay unguarded: taking a customer off a ticket is
            # always allowed, and is exactly what the cleanup cron does.
            if wizard.operation == 'remove' or not wizard.guard_customers:
                continue
            customers = wizard.partner_ids.sudo()._mint_filter_customers()
            if customers:
                raise UserError(_(
                    "These contacts are customers and cannot follow an internal "
                    "ticket:\n\n%(names)s\n\n"
                    "Ticket followers are limited to staff. If one of these is "
                    "actually an employee, mark their contact as an employee "
                    "first, then add them.",
                    names='\n'.join('- %s' % c.display_name for c in customers),
                ))
        return super().edit_followers()
