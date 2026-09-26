# -*- coding: utf-8 -*-
"""Per-deal product photo override for storefront deal cards.

A deal card's product photo is normally borrowed from the first discounted
product that has an image (`borrowedArt` in the frontend's
src/pages/api/everyday-discounts.ts), which is sometimes a brand logo or
category stock art. Marketing uploads `deal_photo` here to pick the photo
themselves; when `deal_photo_url` is set the storefront shows it instead of
the product photo, on /deals and on the daily-deals cards.

The storefront reads `deal_photo_url` straight from Odoo (like brand deal
banners), keyed by `dutchie_discount_id` for Dutchie deals and by record id
for PTL deals -- it does not travel through mintinvsvc/Redis.

Deliberately NOT the older `image` / `image_url` pair on mint.discount:
`image_url` is overwritten from Dutchie's menuDisplayImageUrl by the invsvc
Postgres -> Odoo sync, so an upload there would be clobbered.
"""
import base64
import hashlib
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class MintDiscount(models.Model):
    _inherit = 'mint.discount'

    deal_photo = fields.Binary(
        string='Deal Card Photo', attachment=True, copy=False,
        help='Replaces the product photo on this deal\'s storefront card. '
             'Square works best (the card fits it inside a square slot); '
             'a transparent PNG or WebP cut-out looks cleanest. Uploading '
             'pushes it to the CDN and fills Deal Card Photo URL, which is '
             'what the storefront reads. Clear it to go back to the product '
             'photo.',
    )
    deal_photo_url = fields.Char(
        string='Deal Card Photo URL', readonly=True, copy=False,
        help='CDN copy of the deal card photo; set automatically on upload.',
    )

    def _sync_deal_photo_to_r2(self):
        """Upload deal_photo to Cloudflare R2 and set deal_photo_url.

        Keyed `discounts/<id>/deal-photo-<hash>.<ext>`: the content hash makes
        every replacement a new object, so a CDN or browser cache can never
        keep serving the previous photo under an unchanged URL. Failures are
        logged, not raised, like the brand banner sync: a record with a photo
        and no URL is the visible sign the upload failed.
        """
        self.ensure_one()
        try:
            image_bytes = base64.b64decode(self.deal_photo)
            content_type, ext = self.env['mint.brand']._detect_image_type(image_bytes)
            digest = hashlib.sha1(image_bytes).hexdigest()[:10]
            key = 'discounts/%s/deal-photo-%s.%s' % (self.id, digest, ext)

            from ..utils.r2_upload import upload_to_r2
            url = upload_to_r2(image_bytes, key, content_type)
            super(MintDiscount, self).write({'deal_photo_url': url})
            _logger.info('Synced deal card photo to R2 for discount %s: %s', self.id, url)
        except Exception:
            _logger.exception('Failed to sync deal card photo to R2 for discount %s', self.id)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for record in records:
            if record.deal_photo:
                record._sync_deal_photo_to_r2()
        return records

    def write(self, vals):
        res = super().write(vals)
        if 'deal_photo' in vals:
            if vals['deal_photo']:
                for record in self:
                    record._sync_deal_photo_to_r2()
            else:
                # Cleared: stop the storefront serving the old CDN copy.
                super().write({'deal_photo_url': False})
        return res
