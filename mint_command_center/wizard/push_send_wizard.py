# -*- coding: utf-8 -*-
import logging

from odoo import models, fields, api, _

_logger = logging.getLogger(__name__)

URL_TEMPLATE_MAP = {
    'home': 'https://letsgomint.us',
    'deals': 'https://letsgomint.us/deals',
    'store': 'https://letsgomint.us/stores',
    'menu': 'https://mintdeals.com',
}


class PushSendWizard(models.TransientModel):
    _name = 'mint.push.send.wizard'
    _description = 'Send Push Notification'

    title = fields.Char(string='Title', required=True)
    body = fields.Text(string='Body', required=True)
    url_template = fields.Selection([
        ('custom', 'Custom URL'),
        ('home', 'Home Page'),
        ('deals', "Today's Deals"),
        ('store', 'Store Locator'),
        ('menu', 'Online Menu'),
    ], string='URL Template', default='custom')
    url = fields.Char(string='URL', default='https://letsgomint.us')
    icon = fields.Char(string='Icon URL', default='/favicon.png')
    image = fields.Char(string='Image URL')

    @api.onchange('url_template')
    def _onchange_url_template(self):
        if self.url_template and self.url_template != 'custom':
            self.url = URL_TEMPLATE_MAP.get(self.url_template, 'https://letsgomint.us')

    def action_send(self):
        """Send the notification and create a campaign record."""
        self.ensure_one()

        # Create campaign record
        campaign = self.env['mint.push.campaign'].create({
            'name': self.title,
            'body': self.body,
            'url': self.url,
            'icon': self.icon,
            'image': self.image,
        })

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
