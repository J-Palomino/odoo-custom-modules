# -*- coding: utf-8 -*-
import logging

from odoo import models, fields, api

_logger = logging.getLogger(__name__)


class PushNotification(models.Model):
    _name = 'mint.push.notification'
    _description = 'Push Notification'
    _inherit = ['mail.thread']
    _order = 'create_date desc'

    site_id = fields.Many2one('mint.push.site', string='Site', tracking=True,
                              help='Target a specific site. Leave empty to send to all.')
    name = fields.Char('Title', required=True, tracking=True,
                        default='Mint Cannabis')
    body = fields.Text('Message', required=True, tracking=True)
    url = fields.Char('Click URL', default='/',
                       help='URL to open when the notification is clicked')
    icon = fields.Char('Icon URL', default='/favicon.png')
    image_url = fields.Char('Preview Image URL',
                            help='Large preview image (Chrome/Android)')

    # Action buttons (up to 2, Chrome/Android only)
    action_1_title = fields.Char('Button 1 Label')
    action_1_url = fields.Char('Button 1 URL')
    action_2_title = fields.Char('Button 2 Label')
    action_2_url = fields.Char('Button 2 URL')

    # Targeting
    company_ids = fields.Many2many(
        'res.company', string='Target Stores',
        help='Leave empty to send to all subscribers')

    state = fields.Selection([
        ('draft', 'Draft'),
        ('sending', 'Sending'),
        ('sent', 'Sent'),
        ('failed', 'Failed'),
    ], default='draft', tracking=True, index=True)

    sent_count = fields.Integer('Delivered', readonly=True)
    failed_count = fields.Integer('Failed', readonly=True)
    total_targeted = fields.Integer('Total Targeted', readonly=True)
    sent_at = fields.Datetime('Sent At', readonly=True)

    def _build_payload(self):
        """Build the Web Push JSON payload matching sw.js expectations."""
        self.ensure_one()
        payload = {
            'title': self.name,
            'body': self.body,
            'url': self.url or '/',
            'icon': self.icon or '/favicon.png',
        }
        if self.image_url:
            payload['image'] = self.image_url

        actions = []
        if self.action_1_title and self.action_1_url:
            actions.append({
                'action': 'action1',
                'title': self.action_1_title,
                'url': self.action_1_url,
            })
        if self.action_2_title and self.action_2_url:
            actions.append({
                'action': 'action2',
                'title': self.action_2_title,
                'url': self.action_2_url,
            })
        if actions:
            payload['actions'] = actions

        return payload

    def action_send(self):
        """Send this notification to all active subscribers."""
        self.ensure_one()
        if self.state != 'draft':
            return

        self.write({'state': 'sending'})

        Subscription = self.env['mint.push.subscription'].sudo()
        domain = [('is_active', '=', True)]
        if self.site_id:
            domain.append(('site_id', '=', self.site_id.id))
        subs = Subscription.search(domain)

        payload = self._build_payload()
        sent = 0
        failed = 0

        for sub in subs:
            if sub._send_push(payload):
                sent += 1
            else:
                failed += 1

        self.write({
            'state': 'sent' if sent > 0 else 'failed',
            'sent_count': sent,
            'failed_count': failed,
            'total_targeted': len(subs),
            'sent_at': fields.Datetime.now(),
        })

        _logger.info(
            'Push notification "%s" sent: %d delivered, %d failed out of %d',
            self.name, sent, failed, len(subs),
        )

    def action_reset_to_draft(self):
        """Reset a sent/failed notification to draft for re-sending."""
        self.ensure_one()
        self.write({
            'state': 'draft',
            'sent_count': 0,
            'failed_count': 0,
            'total_targeted': 0,
            'sent_at': False,
        })
