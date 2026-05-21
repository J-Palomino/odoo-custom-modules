import json
import logging
import math
from urllib import error as urlerror, parse as urlparse, request as urlrequest

from odoo import _, api, exceptions, fields, models

_logger = logging.getLogger(__name__)

EARTH_RADIUS_MILES = 3959.0
GOOGLE_PLACES_DETAILS_URL = 'https://maps.googleapis.com/maps/api/place/details/json'
GOOGLE_PLACES_FINDPLACE_URL = 'https://maps.googleapis.com/maps/api/place/findplacefromtext/json'


def _haversine_miles(lat1, lng1, lat2, lng2):
    """Great-circle distance between two lat/lng pairs, in miles."""
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlng / 2) ** 2
    return EARTH_RADIUS_MILES * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class MintCompetitor(models.Model):
    """Competitor dispensary tracked for market intelligence.

    `nearest_company_id` and `distance_from_mint` are computed from the
    competitor's lat/lng against `res.company.partner_id.partner_latitude/
    partner_longitude` of Mint stores in the same market. Both are stored so
    list views can sort by distance without re-running haversine on every read.
    """

    _name = 'mint.competitor'
    _description = 'Competitor Dispensary'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'distance_from_mint, name'

    name = fields.Char(string='Name', required=True, tracking=True)
    market_id = fields.Many2one(
        'mint.region',
        string='Market',
        required=True,
        index=True,
        tracking=True,
    )
    street = fields.Char(string='Street')
    street2 = fields.Char(string='Street 2')
    city = fields.Char(string='City')
    state_id = fields.Many2one(
        'res.country.state',
        string='State',
        index=True,
    )
    zip = fields.Char(string='ZIP')
    latitude = fields.Float(string='Latitude', digits=(10, 7))
    longitude = fields.Float(string='Longitude', digits=(10, 7))
    phone = fields.Char(string='Phone')
    website = fields.Char(string='Website')
    hours = fields.Text(string='Hours')
    nearest_company_id = fields.Many2one(
        'res.company',
        string='Nearest Mint Store',
        compute='_compute_nearest_mint',
        store=True,
    )
    distance_from_mint = fields.Float(
        string='Distance (mi)',
        compute='_compute_nearest_mint',
        store=True,
        digits=(8, 2),
    )
    discovery_source = fields.Selection(
        selection=[
            ('field_visit', 'Field Visit'),
            ('website', 'Website Discovery'),
            ('report', 'Industry Report'),
            ('social', 'Social Media'),
            ('referral', 'Referral'),
            ('other', 'Other'),
        ],
        string='Discovered Via',
        default='other',
    )
    google_place_id = fields.Char(string='Google Place ID', copy=False)
    notes = fields.Text(string='Notes')
    active = fields.Boolean(string='Active', default=True)

    deal_ids = fields.One2many(
        'mint.competitor.deal',
        'competitor_id',
        string='Tracked Deals',
    )
    deal_count = fields.Integer(
        string='Tracked Deals',
        compute='_compute_deal_count',
    )
    active_deal_count = fields.Integer(
        string='Active Deals',
        compute='_compute_deal_count',
    )

    @api.depends('deal_ids', 'deal_ids.is_active')
    def _compute_deal_count(self):
        for rec in self:
            rec.deal_count = len(rec.deal_ids)
            rec.active_deal_count = len(rec.deal_ids.filtered('is_active'))

    @api.depends('latitude', 'longitude', 'market_id')
    def _compute_nearest_mint(self):
        """Pick the Mint store with the smallest haversine distance from this
        competitor. Restricted to stores in the same market_id. If no store in
        the market has coordinates, both fields stay empty so list views can
        flag the data gap."""
        Company = self.env['res.company']
        for rec in self:
            if not (rec.latitude and rec.longitude and rec.market_id):
                rec.nearest_company_id = False
                rec.distance_from_mint = 0.0
                continue
            stores = Company.search([('region_id', '=', rec.market_id.id)])
            best_store, best_dist = False, None
            for store in stores:
                slat = store.partner_id.partner_latitude or 0.0
                slng = store.partner_id.partner_longitude or 0.0
                if not slat or not slng:
                    continue
                d = _haversine_miles(rec.latitude, rec.longitude, slat, slng)
                if best_dist is None or d < best_dist:
                    best_store, best_dist = store, d
            rec.nearest_company_id = best_store
            rec.distance_from_mint = round(best_dist, 2) if best_dist is not None else 0.0

    def action_update_from_google_places(self):
        """Look up this competitor on Google Places by name + city/state and
        backfill phone, website, lat/lng, place_id. Requires
        `ir.config_parameter` `mint_command_center.google_places_api_key`."""
        self.ensure_one()
        key = self.env['ir.config_parameter'].sudo().get_param(
            'mint_command_center.google_places_api_key'
        )
        if not key:
            raise exceptions.UserError(_(
                'Google Places API key is not configured.\n'
                'Set ir.config_parameter `mint_command_center.google_places_api_key` '
                'to enable this button.'
            ))

        query_parts = [self.name]
        if self.city:
            query_parts.append(self.city)
        if self.state_id:
            query_parts.append(self.state_id.code or self.state_id.name)
        query = ' '.join(query_parts)

        place_id = self.google_place_id
        if not place_id:
            place_id = self._google_findplace(query, key)
            if not place_id:
                raise exceptions.UserError(_('Google Places returned no match for "%s".') % query)

        details = self._google_place_details(place_id, key)
        if not details:
            raise exceptions.UserError(_('Google Places details lookup failed for place_id=%s.') % place_id)

        vals = {'google_place_id': place_id}
        result = details.get('result') or {}
        if result.get('formatted_phone_number') and not self.phone:
            vals['phone'] = result['formatted_phone_number']
        if result.get('website') and not self.website:
            vals['website'] = result['website']
        geo = (result.get('geometry') or {}).get('location') or {}
        if geo.get('lat') is not None:
            vals['latitude'] = geo['lat']
        if geo.get('lng') is not None:
            vals['longitude'] = geo['lng']
        opening = result.get('opening_hours') or {}
        weekday = opening.get('weekday_text')
        if weekday and not self.hours:
            vals['hours'] = '\n'.join(weekday)

        self.write(vals)
        self.message_post(body=_(
            'Updated from Google Places (place_id=%s). Fields refreshed: %s.'
        ) % (place_id, ', '.join(k for k in vals if k != 'google_place_id') or 'none'))
        return True

    def _google_findplace(self, query, key):
        params = urlparse.urlencode({
            'input': query,
            'inputtype': 'textquery',
            'fields': 'place_id',
            'key': key,
        })
        return self._google_call(f'{GOOGLE_PLACES_FINDPLACE_URL}?{params}', extract='findplace')

    def _google_place_details(self, place_id, key):
        params = urlparse.urlencode({
            'place_id': place_id,
            'fields': 'name,formatted_phone_number,website,geometry/location,opening_hours/weekday_text',
            'key': key,
        })
        return self._google_call(f'{GOOGLE_PLACES_DETAILS_URL}?{params}', extract='details')

    def _google_call(self, url, extract):
        try:
            req = urlrequest.Request(url, headers={'User-Agent': 'mint-command-center/1.0'})
            with urlrequest.urlopen(req, timeout=10) as resp:
                payload = json.loads(resp.read().decode('utf-8'))
        except (urlerror.URLError, json.JSONDecodeError) as e:
            _logger.warning('Google Places call failed: %s', e)
            return None
        if payload.get('status') not in ('OK', 'ZERO_RESULTS'):
            _logger.warning('Google Places returned status=%s error=%s',
                            payload.get('status'), payload.get('error_message'))
        if extract == 'findplace':
            candidates = payload.get('candidates') or []
            return candidates[0]['place_id'] if candidates else None
        return payload

    def action_view_deals(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': f'Deals — {self.name}',
            'res_model': 'mint.competitor.deal',
            'view_mode': 'list,form',
            'domain': [('competitor_id', '=', self.id)],
            'context': {'default_competitor_id': self.id},
        }
