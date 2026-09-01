# -*- coding: utf-8 -*-
import logging

from odoo import api, models

_logger = logging.getLogger(__name__)

# Models whose followers must stay internal-only. Overridable without a code
# change via the ir.config_parameter of the same name (comma-separated).
GUARD_MODELS_PARAM = 'mint_customer_api.ticket_follower_guard_models'
GUARD_MODELS_DEFAULT = 'maintenance.request,project.task'


class MailThread(models.AbstractModel):
    """Keep customers off the follower list of internal-only documents.

    Maintenance requests and project tasks are internal work tickets
    (IT/Engineering/Facilities). A customer partner has no business
    following one, and when it happens the ticket becomes unopenable: the
    customer-isolation rule hides that partner from internal users, so
    rendering the follower avatars raises AccessError.

    ``mint_maintenance_form`` already ships a 15-minute cron that unfollows
    customers after the fact. This is the preventive half — it stops the
    subscription from being recorded at all, so the window in which a ticket
    is broken never opens.

    The drop is silent rather than an exception on purpose: this method sits
    on the path the mail gateway, automated actions and the REST API all
    take, and a customer replying to a ticket alias must not raise. The
    interactive path (the Add Followers dialog) raises a real error instead —
    see ``mail_followers_edit.py`` — so a human who tries it is told why.
    """

    _inherit = 'mail.thread'

    @api.model
    def _mint_follower_guard_models(self):
        param = self.env['ir.config_parameter'].sudo().get_param(
            GUARD_MODELS_PARAM, GUARD_MODELS_DEFAULT)
        return {m.strip() for m in (param or '').split(',') if m.strip()}

    @api.model
    def _mint_follower_guard_active(self):
        return self._name in self._mint_follower_guard_models()

    def _mint_reject_customer_followers(self, partner_ids):
        """Return partner_ids with genuine customers removed, logging each drop."""
        if not partner_ids or not self._mint_follower_guard_active():
            return partner_ids
        partners = self.env['res.partner'].sudo().browse(partner_ids)
        customers = partners._mint_filter_customers()
        if not customers:
            return partner_ids
        dropped = set(customers.ids)
        _logger.info(
            "Ticket-follower guard: refused to subscribe customer partner(s) %s "
            "to %s %s", sorted(dropped), self._name, self.ids)
        return [pid for pid in partner_ids if pid not in dropped]

    def message_subscribe(self, partner_ids=None, *args, **kwargs):
        partner_ids = self._mint_reject_customer_followers(partner_ids)
        if partner_ids == []:
            # Every requested follower was a customer, so there is nothing
            # left to subscribe. Odoo's own message_subscribe already no-ops
            # on an empty list; returning here just makes that explicit
            # rather than depending on it.
            return True
        return super().message_subscribe(partner_ids, *args, **kwargs)
