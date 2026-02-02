# -*- coding: utf-8 -*-
"""
Event model for store events and promotions.
"""
from odoo import api, fields, models
from datetime import datetime


class MintEvent(models.Model):
    _name = "mint.event"
    _description = "Store Event"
    _order = "start_datetime asc"

    title = fields.Char(string="Title", required=True)
    slug = fields.Char(string="URL Slug", index=True)

    # Content
    description = fields.Html(string="Description")

    # Schedule
    start_datetime = fields.Datetime(string="Start Date/Time", required=True)
    end_datetime = fields.Datetime(string="End Date/Time", required=True)
    is_all_day = fields.Boolean(string="All Day Event", default=False)

    # Recurrence
    is_recurring = fields.Boolean(string="Recurring Event", default=False)
    recurrence_rule = fields.Char(string="Recurrence Rule")

    # Location
    store_id = fields.Many2one(
        'res.company',
        string="Store",
        domain=[('is_dispensary', '=', True)],
        required=True
    )
    location = fields.Char(string="Location Details")

    # Media
    image = fields.Binary(string="Event Image")
    image_url = fields.Char(string="Event Image URL")

    # Event type
    event_type = fields.Selection([
        ('vendor_day', 'Vendor Day'),
        ('special_sale', 'Special Sale'),
        ('workshop', 'Workshop'),
        ('community', 'Community Event'),
        ('holiday', 'Holiday Event'),
        ('other', 'Other'),
    ], string="Event Type", default='other')

    # Status
    is_featured = fields.Boolean(string="Featured", default=False)
    is_published = fields.Boolean(string="Published", default=True)

    @api.model
    def get_upcoming_events(self, store_id=None, limit=None):
        """Return upcoming events, optionally filtered by store."""
        domain = [
            ('is_published', '=', True),
            ('end_datetime', '>=', datetime.now()),
        ]
        if store_id:
            domain.append(('store_id', '=', store_id))
        return self.search(domain, limit=limit, order="start_datetime asc")

    @api.model
    def get_store_events(self, store_id, include_past=False):
        """Return events for a specific store."""
        domain = [
            ('is_published', '=', True),
            ('store_id', '=', store_id),
        ]
        if not include_past:
            domain.append(('end_datetime', '>=', datetime.now()))
        return self.search(domain, order="start_datetime asc")
