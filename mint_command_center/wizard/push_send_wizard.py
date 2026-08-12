# -*- coding: utf-8 -*-
import logging

from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

URL_TEMPLATE_MAP = {
    'home': 'https://letsgomint.us',
    'deals': 'https://letsgomint.us/deals',
    'store': 'https://letsgomint.us/stores',
    'menu': 'https://mintdeals.com',
}


class PushSendWizardExt(models.TransientModel):
    _inherit = 'mint.push.send.wizard'

    # Additional targeting fields
    company_ids = fields.Many2many(
        'res.company', string='Target Stores',
        domain="[('parent_id', '!=', False)]",
        help='Leave empty to send to all subscribers')
    region = fields.Char(string='Region',
        help='Filter by region slug (e.g., "arizona")')
    url_template = fields.Selection([
        ('custom', 'Custom URL'),
        ('home', 'Home Page'),
        ('deals', "Today's Deals"),
        ('store', 'Store Locator'),
        ('menu', 'Online Menu'),
    ], string='URL Template', default='custom')

    @api.onchange('url_template')
    def _onchange_url_template(self):
        if self.url_template and self.url_template != 'custom':
            self.url = URL_TEMPLATE_MAP.get(self.url_template, 'https://letsgomint.us')

    def action_send(self):
        """Send the notification and create a campaign record."""
        self.ensure_one()

        # A "Specific Contact" send must never reach the campaign path below:
        # campaigns filter only by store/region, so it would broadcast to every
        # subscriber. Route it through the base wizard's send_to_partner().
        if self.target == 'partner':
            if not self.partner_id:
                raise UserError(_("Select a contact to send to."))
            return super().action_send()

        # Create campaign record
        campaign_vals = {
            'name': self.title,
            'body': self.body,
            'url': self.url,
            'icon': self.icon,
            'image': self.image,
        }
        if self.company_ids:
            campaign_vals['company_ids'] = [(6, 0, self.company_ids.ids)]
        if self.region:
            campaign_vals['region'] = self.region
        campaign = self.env['mint.push.campaign'].create(campaign_vals)

        # Send it
        campaign.send_notification()

        # Show success notification
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Push Notification Sent"),
                'message': _("Delivered to %d of %d subscribers.") % (
                    campaign.sent_count, campaign.total_count),
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }
