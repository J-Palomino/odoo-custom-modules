# -*- coding: utf-8 -*-
"""
Store model - extends res.company for dispensary locations.
Each company represents a store location with cannabis-specific fields.
"""
import base64
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class ResCompany(models.Model):
    # NOTE: We previously inherited website.seo.metadata to get the standard
    # meta_title/description/keywords/og_img fields, but that mixin pulls in
    # `is_seo_optimized` (a stored computed column). The auto_init that should
    # create the column runs in a phase that itself queries res.company —
    # chicken-and-egg, leaving res.company unqueryable. Use plain custom
    # fields (x_seo_*) instead.
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

    # ===== SEO (custom fields — see note at class top about why we don't
    # inherit website.seo.metadata) =====
    # Templates accept tokens: {store_name} {city} {state} {state_short}
    # {region} {zip} {phone} {brand} {year}. Fields left blank fall back to
    # frontend defaults (current "{Store} - {City}, {State}" pattern).
    x_seo_title = fields.Char(
        string="SEO Title",
        help="Page <title> tag. Supports template tokens.",
    )
    x_seo_description = fields.Text(
        string="SEO Description",
        help="<meta name='description'>. Supports template tokens.",
    )
    x_seo_keywords = fields.Char(
        string="SEO Keywords",
        help="<meta name='keywords'>. Supports template tokens.",
    )
    x_seo_h1 = fields.Char(
        string="H1 Heading",
        help="Visible page heading. Falls back to store name when empty.",
    )
    x_seo_canonical_url = fields.Char(
        string="Canonical URL Override",
        help="Force a canonical URL. Leave blank to use the page URL.",
    )
    x_seo_robots = fields.Selection(
        [
            ('index,follow', 'Index, Follow (default)'),
            ('noindex,follow', 'No Index, Follow'),
            ('index,nofollow', 'Index, No Follow'),
            ('noindex,nofollow', 'No Index, No Follow'),
        ],
        string="Robots Directive",
        default='index,follow',
        help="Hide soft-launched / pre-opening stores from search engines.",
    )
    x_seo_schema_type = fields.Selection(
        [
            ('Store', 'Store (default)'),
            ('LocalBusiness', 'LocalBusiness'),
            ('HealthAndBeautyBusiness', 'HealthAndBeautyBusiness'),
            ('Pharmacy', 'Pharmacy'),
        ],
        string="Schema.org Type",
        default='Store',
    )
    x_seo_price_range = fields.Char(
        string="Price Range",
        help="Used in LocalBusiness JSON-LD (e.g. '$$').",
    )
    x_seo_payment_accepted = fields.Char(
        string="Payment Accepted",
        help="Comma-separated list (e.g. 'Cash, Debit, CanPay').",
    )
    x_seo_same_as = fields.Text(
        string="Social Profile URLs",
        help=(
            "One URL per line. Feeds JSON-LD sameAs[] so Google links the "
            "knowledge panel to your social profiles."
        ),
    )
    x_seo_og_image = fields.Binary(
        string="Social Share Image (Upload)",
        help="Recommended 1200x630. Auto-uploaded to R2 on save.",
    )
    x_seo_og_image_url = fields.Char(
        string="Social Share Image URL",
        help=(
            "Auto-populated when an image is uploaded above. Or paste a "
            "CDN-hosted URL directly when stores host their own share image."
        ),
    )

    def write(self, vals):
        res = super().write(vals)
        if 'hero_image' in vals:
            for company in self:
                if company.hero_image and company.slug:
                    company._sync_hero_to_r2()
                elif not company.hero_image:
                    super(ResCompany, company).write({'hero_image_url': False})
        # Auto-sync logo changes to hero_image (so Trish can upload via the logo field)
        if 'logo' in vals and 'hero_image' not in vals:
            for company in self:
                if company.logo and company.slug and company.is_dispensary:
                    super(ResCompany, company).write({'hero_image': company.logo})
                    company._sync_hero_to_r2()
        # If slug changed and hero_image exists but no URL yet, upload
        if 'slug' in vals:
            for company in self:
                if company.hero_image and company.slug and not company.hero_image_url:
                    company._sync_hero_to_r2()
        # SEO OG image — upload binary to R2 and write back URL
        if 'x_seo_og_image' in vals:
            for company in self:
                if company.x_seo_og_image and company.slug:
                    company._sync_seo_og_to_r2()
                elif not company.x_seo_og_image:
                    # Only clear the URL if it looks R2-managed; preserve manually-pasted CDN URLs
                    if company.x_seo_og_image_url and '/seo-og.' in company.x_seo_og_image_url:
                        super(ResCompany, company).write({'x_seo_og_image_url': False})
        return res

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for record in records:
            if record.hero_image and record.slug:
                record._sync_hero_to_r2()
            if record.x_seo_og_image and record.slug:
                record._sync_seo_og_to_r2()
        return records

    def _sync_hero_to_r2(self):
        """Upload hero_image to Cloudflare R2 and set hero_image_url."""
        self.ensure_one()
        try:
            image_bytes = base64.b64decode(self.hero_image)
            content_type, ext = self._detect_image_type(image_bytes)
            key = f"stores/{self.slug}/hero.{ext}"

            from ..utils.r2_upload import upload_to_r2
            url = upload_to_r2(image_bytes, key, content_type)
            super(ResCompany, self).write({'hero_image_url': url})
            _logger.info("Synced hero image to R2 for %s: %s", self.slug, url)
        except Exception:
            _logger.exception("Failed to sync hero image to R2 for %s", self.slug)

    def _sync_seo_og_to_r2(self):
        """Upload x_seo_og_image to Cloudflare R2 and set x_seo_og_image_url."""
        self.ensure_one()
        try:
            image_bytes = base64.b64decode(self.x_seo_og_image)
            content_type, ext = self._detect_image_type(image_bytes)
            key = f"stores/{self.slug}/seo-og.{ext}"

            from ..utils.r2_upload import upload_to_r2
            url = upload_to_r2(image_bytes, key, content_type)
            super(ResCompany, self).write({'x_seo_og_image_url': url})
            _logger.info("Synced SEO OG image to R2 for %s: %s", self.slug, url)
        except Exception:
            _logger.exception("Failed to sync SEO OG image to R2 for %s", self.slug)

    @staticmethod
    def _detect_image_type(image_bytes):
        """Detect image MIME type and extension from magic bytes."""
        if image_bytes[:3] == b'\xff\xd8\xff':
            return 'image/jpeg', 'jpg'
        if image_bytes[:4] == b'RIFF':
            return 'image/webp', 'webp'
        return 'image/png', 'png'

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
    slug = fields.Char(string="URL Slug", index=True)
    hero_image = fields.Binary(string="Hero Image")
    hero_image_url = fields.Char(string="Hero Image URL")
    store_ids = fields.One2many('res.company', 'region_id', string="Stores")
    store_count = fields.Integer(string="Stores", compute="_compute_store_count")

    def _compute_store_count(self):
        for region in self:
            region.store_count = len(region.store_ids.filtered(
                lambda c: getattr(c, 'is_dispensary', False) and getattr(c, 'is_active', True)
            ))

    def write(self, vals):
        res = super().write(vals)
        if 'hero_image' in vals:
            for region in self:
                if region.hero_image and region.slug:
                    region._sync_hero_to_r2()
                elif not region.hero_image:
                    super(MintRegion, region).write({'hero_image_url': False})
        if 'slug' in vals:
            for region in self:
                if region.hero_image and region.slug and not region.hero_image_url:
                    region._sync_hero_to_r2()
        return res

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for record in records:
            if record.hero_image and record.slug:
                record._sync_hero_to_r2()
        return records

    def _sync_hero_to_r2(self):
        """Upload hero_image to Cloudflare R2 and set hero_image_url."""
        self.ensure_one()
        try:
            image_bytes = base64.b64decode(self.hero_image)
            content_type, ext = ResCompany._detect_image_type(image_bytes)
            key = f"regions/{self.slug}/hero.{ext}"

            from ..utils.r2_upload import upload_to_r2
            url = upload_to_r2(image_bytes, key, content_type)
            super(MintRegion, self).write({'hero_image_url': url})
            _logger.info("Synced hero image to R2 for region %s: %s", self.slug, url)
        except Exception:
            _logger.exception("Failed to sync hero image to R2 for region %s", self.slug)


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
