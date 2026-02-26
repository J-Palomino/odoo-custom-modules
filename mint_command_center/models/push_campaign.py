# -*- coding: utf-8 -*-
import logging

from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class PushCampaign(models.Model):
    _name = 'mint.push.campaign'
    _description = 'Push Notification Campaign'
    _inherit = ['mail.thread']
    _order = 'sent_at desc, id desc'

    name = fields.Char(string='Title', required=True, tracking=True)
    body = fields.Text(string='Body', required=True, tracking=True)
    url = fields.Char(string='URL', tracking=True)
    icon = fields.Char(string='Icon URL')
    image = fields.Char(string='Image URL')
    sent_count = fields.Integer(string='Delivered', readonly=True)
    total_count = fields.Integer(string='Total Subscriptions', readonly=True)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('sent', 'Sent'),
        ('failed', 'Failed'),
    ], string='Status', default='draft', readonly=True, tracking=True)
    sent_at = fields.Datetime(string='Sent At', readonly=True)
    sent_by = fields.Many2one('res.users', string='Sent By', readonly=True)

    def send_notification(self):
        """Send the push notification and record the result."""
        self.ensure_one()
        if self.state == 'sent':
            raise UserError(_("This notification has already been sent."))

        Sub = self.env['mint.push.subscription'].sudo()
        total = Sub.search_count([])

        try:
            sent = Sub.send_to_all(
                title=self.name,
                body=self.body,
                url=self.url or None,
                icon=self.icon or None,
                image=self.image or None,
            )
            self.write({
                'sent_count': sent,
                'total_count': total,
                'state': 'sent',
                'sent_at': fields.Datetime.now(),
                'sent_by': self.env.uid,
            })
            _logger.info("Campaign '%s' sent to %d/%d", self.name, sent, total)
        except Exception as e:
            self.write({
                'state': 'failed',
                'total_count': total,
                'sent_at': fields.Datetime.now(),
                'sent_by': self.env.uid,
            })
            _logger.exception("Campaign '%s' failed: %s", self.name, e)
            raise UserError(_("Failed to send notification: %s") % str(e))
