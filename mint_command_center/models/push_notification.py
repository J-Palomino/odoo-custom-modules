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
    targeting_mode = fields.Selection([
        ('all', 'All Subscribers'),
        ('stores', 'By Store(s)'),
        ('proximity', 'By Proximity'),
    ], string='Targeting', default='all', required=True)
    company_ids = fields.Many2many(
        'res.company', string='Target Stores',
        help='Select stores to target. Only used when targeting = By Store(s).')
    proximity_store_id = fields.Many2one(
        'res.company', string='Near Store',
        domain="[('parent_id', '!=', False)]",
        help='Center point for proximity targeting.')
    proximity_radius = fields.Float('Radius (miles)', default=10.0)
    estimated_audience = fields.Integer(
        string='Est. Audience', compute='_compute_estimated_audience')

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

    @api.depends('site_id', 'targeting_mode', 'company_ids', 'proximity_store_id', 'proximity_radius')
    def _compute_estimated_audience(self):
        Sub = self.env['mint.push.subscription'].sudo()
        for rec in self:
            if rec.targeting_mode == 'proximity' and rec.proximity_store_id:
                # Count via Haversine SQL
                partner = rec.proximity_store_id.partner_id
                lat = partner.partner_latitude
                lng = partner.partner_longitude
                radius = rec.proximity_radius or 10.0
                if lat and lng:
                    self.env.cr.execute("""
                        SELECT COUNT(*) FROM mint_push_subscription
                        WHERE latitude IS NOT NULL AND longitude IS NOT NULL
                          AND (3959 * acos(
                                cos(radians(%s)) * cos(radians(latitude))
                                * cos(radians(longitude) - radians(%s))
                                + sin(radians(%s)) * sin(radians(latitude))
                          )) <= %s
                    """, (lat, lng, lat, radius))
                    rec.estimated_audience = self.env.cr.fetchone()[0]
                else:
                    rec.estimated_audience = 0
            else:
                domain = [('is_active', '=', True)]
                if rec.site_id:
                    domain.append(('site_id', '=', rec.site_id.id))
                if rec.targeting_mode == 'stores' and rec.company_ids:
                    domain.append(('store_id', 'in', rec.company_ids.ids))
                rec.estimated_audience = Sub.search_count(domain)

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
        """Send this notification based on targeting mode."""
        self.ensure_one()
        if self.state != 'draft':
            return

        self.write({'state': 'sending'})

        Subscription = self.env['mint.push.subscription'].sudo()
        payload = self._build_payload()
        site_id = self.site_id.id if self.site_id else None

        if self.targeting_mode == 'proximity' and self.proximity_store_id:
            partner = self.proximity_store_id.partner_id
            lat = partner.partner_latitude
            lng = partner.partner_longitude
            radius = self.proximity_radius or 10.0
            if lat and lng:
                sent = Subscription.send_to_nearby(
                    lat=lat, lng=lng, radius_miles=radius,
                    title=payload['title'],
                    body=payload['body'],
                    url=payload.get('url'),
                    icon=payload.get('icon'),
                    image=payload.get('image'),
                    actions=payload.get('actions'),
                )
            else:
                sent = 0
                _logger.warning("Proximity store %s has no GPS coordinates", self.proximity_store_id.name)
            total = self.estimated_audience
        else:
            company_ids = self.company_ids.ids if self.targeting_mode == 'stores' and self.company_ids else None
            sent = Subscription.send_to_all(
                title=payload['title'],
                body=payload['body'],
                url=payload.get('url'),
                icon=payload.get('icon'),
                image=payload.get('image'),
                actions=payload.get('actions'),
                site_id=site_id,
                company_ids=company_ids,
            )

            domain = [('site_id', '=', site_id)] if site_id else []
            if company_ids:
                domain.append(('store_id', 'in', company_ids))
            total = Subscription.search_count(domain)

        failed = max(total - sent, 0)

        self.write({
            'state': 'sent' if sent > 0 else 'failed',
            'sent_count': sent,
            'failed_count': failed,
            'total_targeted': total,
            'sent_at': fields.Datetime.now(),
        })

        _logger.info(
            'Push notification "%s" [%s] sent: %d delivered, %d failed out of %d',
            self.name, self.targeting_mode, sent, failed, total,
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
