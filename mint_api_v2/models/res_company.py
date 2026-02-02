# -*- coding: utf-8 -*-
"""
Store model - extends res.company for dispensary locations.
Each company represents a store location with cannabis-specific fields.
"""
from odoo import api, fields, models


class ResCompany(models.Model):
    _inherit = "res.company"

    # Store identification
    slug = fields.Char(string="URL Slug", index=True)
    store_code = fields.Char(string="Store Code")
    is_dispensary = fields.Boolean(string="Is Dispensary", default=False)
    is_active = fields.Boolean(string="Active", default=True)

    # Dutchie integration
    dutchie_store_id = fields.Char(string="Dutchie Store ID")
    dutchie_api_key = fields.Char(string="Dutchie API Key")
    menu_url = fields.Text(string="Menu Embed URL")
    online_ordering_url = fields.Text(string="Online Ordering URL")
    pos_location_id = fields.Char(string="POS Location ID")

    # Store type
    is_medical = fields.Boolean(string="Medical", default=False)
    is_recreational = fields.Boolean(string="Recreational", default=True)
    has_cafe = fields.Boolean(string="Has Cafe", default=False)
    is_24hours = fields.Boolean(string="24 Hours", default=False)

    # License
    license_number = fields.Char(string="License Number")

    # Location
    latitude = fields.Float(string="Latitude", digits=(10, 7))
    longitude = fields.Float(string="Longitude", digits=(10, 7))
    google_place_id = fields.Char(string="Google Place ID")
    google_map_embed = fields.Text(string="Google Map Embed")
    google_map_link = fields.Char(string="Google Map Link")

    # Content
    summary = fields.Text(string="Summary")
    about = fields.Text(string="About")
    description = fields.Text(string="Description")
    tickertape = fields.Text(string="Ticker Tape Message")

    # Images
    hero_image = fields.Binary(string="Hero Image")
    hero_image_url = fields.Char(string="Hero Image URL")

    # Hours (stored as "HH:MM-HH:MM" or "closed")
    hours_monday = fields.Char(string="Monday Hours", default="09:00-21:00")
    hours_tuesday = fields.Char(string="Tuesday Hours", default="09:00-21:00")
    hours_wednesday = fields.Char(string="Wednesday Hours", default="09:00-21:00")
    hours_thursday = fields.Char(string="Thursday Hours", default="09:00-21:00")
    hours_friday = fields.Char(string="Friday Hours", default="09:00-21:00")
    hours_saturday = fields.Char(string="Saturday Hours", default="09:00-21:00")
    hours_sunday = fields.Char(string="Sunday Hours", default="10:00-18:00")

    # Relations
    region_id = fields.Many2one('mint.region', string="Region")
    amenity_ids = fields.Many2many('mint.amenity', string="Amenities")
    service_ids = fields.Many2many('mint.service', string="Services")

    @api.model
    def get_active_stores(self):
        """Return all active dispensary stores."""
        return self.search([
            ('is_dispensary', '=', True),
            ('is_active', '=', True),
        ])

    def get_hours_dict(self):
        """Return hours as a structured dictionary."""
        self.ensure_one()
        return {
            'monday': self._parse_hours(self.hours_monday),
            'tuesday': self._parse_hours(self.hours_tuesday),
            'wednesday': self._parse_hours(self.hours_wednesday),
            'thursday': self._parse_hours(self.hours_thursday),
            'friday': self._parse_hours(self.hours_friday),
            'saturday': self._parse_hours(self.hours_saturday),
            'sunday': self._parse_hours(self.hours_sunday),
        }

    def _parse_hours(self, hours_str):
        """Parse hours string into open/close times."""
        if not hours_str or hours_str.lower() == 'closed':
            return {'open': None, 'close': None, 'is_closed': True}
        try:
            open_time, close_time = hours_str.split('-')
            return {
                'open': open_time.strip(),
                'close': close_time.strip(),
                'is_closed': False,
            }
        except ValueError:
            return {'open': None, 'close': None, 'is_closed': True}


class MintRegion(models.Model):
    _name = "mint.region"
    _description = "Store Region"

    name = fields.Char(string="Name", required=True)
    code = fields.Char(string="Code")


class MintAmenity(models.Model):
    _name = "mint.amenity"
    _description = "Store Amenity"

    name = fields.Char(string="Name", required=True)
    icon = fields.Char(string="Icon")


class MintService(models.Model):
    _name = "mint.service"
    _description = "Store Service"

    name = fields.Char(string="Name", required=True)
    icon = fields.Char(string="Icon")
