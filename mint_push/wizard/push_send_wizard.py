# -*- coding: utf-8 -*-
import logging

from odoo import models, fields, api

_logger = logging.getLogger(__name__)


class PushSendWizard(models.TransientModel):
    _name = 'mint.push.send.wizard'
    _description = 'Send Push Notification'

    site_id = fields.Many2one('mint.push.site', string='Site',
                              help='Send only to subscribers of this site. '
                                   'Leave empty to send to all sites.')
    title = fields.Char(string='Title', required=True)
    body = fields.Text(string='Message', required=True)
    url = fields.Char(string='Click URL', default='https://letsgomint.us',
                      help='URL opened when user taps the notification')
    icon = fields.Char(string='Icon URL', default='/favicon.png')
    image = fields.Char(string='Image URL',
                        help='Large preview image shown in the notification')
    target = fields.Selection([
        ('all', 'All Subscribers'),
        ('partner', 'Specific Contact'),
    ], string='Send To', default='all', required=True)
    partner_id = fields.Many2one('res.partner', string='Contact')

    def action_send(self):
        """Send the push notification."""
        self.ensure_one()
        Sub = self.env['mint.push.subscription'].sudo()
        site_id = self.site_id.id if self.site_id else False

        if self.target == 'partner' and self.partner_id:
            sent = Sub.send_to_partner(
                self.partner_id.id,
                self.title, self.body,
                url=self.url, icon=self.icon, image=self.image,
                site_id=site_id,
            )
        else:
            sent = Sub.send_to_all(
                self.title, self.body,
                url=self.url, icon=self.icon, image=self.image,
                site_id=site_id,
            )

        site_label = self.site_id.name if self.site_id else 'all sites'
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Push Notification Sent',
                'message': f'Delivered to {sent} subscriber(s) on {site_label}.',
                'type': 'success',
                'sticky': False,
            },
        }
