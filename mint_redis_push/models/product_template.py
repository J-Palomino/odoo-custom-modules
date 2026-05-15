# -*- coding: utf-8 -*-
"""
Extend product.template with Odoo → Redis override push hooks.

Only three fields currently override-eligible per product requirements:
  - price (rec_price, med_price, list_price — any change enqueues)
  - name
  - image (image_1920; pushed as a stable /web/image URL)

The override *layer* pattern: Dutchie remains authoritative for stock +
identity; Odoo overrides ride on top when present. Clearing the Odoo field
in Odoo reverts — mintinvsvc treats an override entry with null value as
"remove this override, serve Dutchie's value again".
"""
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# Fields whose change triggers a product-override push. If you add a field
# here, also teach mintinvsvc's override-apply layer to recognize the key.
OVERRIDE_FIELDS = frozenset({
    'list_price',   # Odoo's canonical price field (inherited)
    'rec_price',    # mint_api_v2 Rec Price Float
    'med_price',    # mint_api_v2 Med Price Float
    'name',
    'image_1920',
})


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    def write(self, vals):
        res = super().write(vals)
        touched = set(vals.keys()) & OVERRIDE_FIELDS
        if touched:
            # Only products with a Dutchie id are reachable from the FE —
            # non-mirrored Odoo products don't appear in mintinvsvc inventory
            # so an override would have nothing to attach to.
            dutchie_ids = [r.dutchie_product_id for r in self if r.dutchie_product_id]
            if dutchie_ids:
                self.env['mint.redis.push.queue'].sudo()._enqueue_product_override(dutchie_ids)
        return res

    # ── Serialization ────────────────────────────────────────────────

    def _serialize_override_for_redis(self):
        """Emit override fields for this product. Null fields are explicit —
        mintinvsvc interprets null as "drop override; serve Dutchie value".
        The base URL for images comes from Odoo's own /web/image endpoint so
        we don't have to re-host in Redis or upload elsewhere."""
        self.ensure_one()
        ICP = self.env['ir.config_parameter'].sudo()
        odoo_base = ICP.get_param('web.base.url', '').rstrip('/')
        image_url = (
            f"{odoo_base}/web/image/product.template/{self.id}/image_1920"
            if self.image_1920 else None
        )
        # Prefer rec_price then med_price then list_price. FE only shows one
        # price per SKU — pick the most storefront-relevant.
        price = self.rec_price or self.med_price or self.list_price or None

        return {
            'dutchie_product_id': self.dutchie_product_id,
            'odoo_product_template_id': self.id,
            'fields': {
                'price': price,
                'name': self.name or None,
                'image_url': image_url,
            },
            'updated_at': fields.Datetime.to_string(fields.Datetime.now()),
        }

    @api.model
    def _push_override_by_dutchie_id(self, dutchie_product_id):
        """Queue worker entry point — push override for a single product."""
        rec = self.sudo().search([
            ('dutchie_product_id', '=', str(dutchie_product_id)),
        ], limit=1)
        if not rec:
            _logger.warning('Override push: no product.template with dutchie_product_id=%s',
                            dutchie_product_id)
            return
        payload = rec._serialize_override_for_redis()
        Queue = self.env['mint.redis.push.queue'].sudo()
        status, body = Queue._post('/api/products/overrides', {'overrides': [payload]})
        if status >= 300:
            raise RuntimeError(f'override push got {status}: {body[:200]}')
        _logger.info('Pushed override for product %s (dutchie_id=%s, HTTP %s)',
                     rec.name, dutchie_product_id, status)

    # ── Admin action ─────────────────────────────────────────────────

    def action_push_override_to_redis(self):
        """Admin-button push. Immediate, not debounced."""
        dutchie_ids = [r.dutchie_product_id for r in self if r.dutchie_product_id]
        if dutchie_ids:
            self.env['mint.redis.push.queue'].sudo()._enqueue_product_override(dutchie_ids)
            self.env['mint.redis.push.queue'].sudo()._cron_process()
        return True
