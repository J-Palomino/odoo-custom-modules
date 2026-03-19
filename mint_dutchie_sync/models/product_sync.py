# -*- coding: utf-8 -*-
"""
Real-time Odoo → Dutchie product sync.

When a user saves a product in the Odoo UI, this override detects the change
and pushes updated fields to the Dutchie Backoffice API via the inventory
service webhook.

Automated sync writes pass context={'tracking_disable': True} and are skipped
to avoid circular updates.
"""
import json
import logging
import threading

from odoo import api, models

_logger = logging.getLogger(__name__)

# Odoo field → Dutchie Backoffice field (PascalCase)
FIELD_MAP = {
    'name': 'Name',
    'default_code': 'Sku',
    'x_brand': 'BrandName',
    'x_category': 'Category',
    'description_sale': 'Description',
    'list_price': 'Price',
    'standard_price': 'UnitCost',
    'x_strain': 'Strain',
    'x_strain_type': 'StrainType',
    'x_thc': 'THC',
    'x_cbd': 'CBD',
    'x_rec_price': 'RecPrice',
    'x_med_price': 'MedPrice',
    'x_weight_grams': 'NetWeight',
}

# Fields we care about — if none of these changed, skip the push
WATCHED_FIELDS = set(FIELD_MAP.keys())

# Inventory service webhook URL (Railway internal or public)
WEBHOOK_URL_PARAM = 'mint.dutchie_sync.webhook_url'
DEFAULT_WEBHOOK_URL = 'https://mintinvsvc-production.up.railway.app/api/webhook/odoo-product-change'


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    def write(self, vals):
        res = super().write(vals)

        # Skip if this is an automated sync write (quiet mode)
        if self.env.context.get('tracking_disable'):
            return res

        # Skip if no watched fields changed
        changed = WATCHED_FIELDS & set(vals.keys())
        if not changed:
            return res

        # Fire webhook in background thread to avoid blocking the UI save
        for record in self:
            dutchie_id = record.x_dutchie_product_id if hasattr(record, 'x_dutchie_product_id') else None
            if not dutchie_id:
                continue

            # Build payload with only changed fields
            payload = {
                'model': 'product.template',
                'odoo_id': record.id,
                'dutchie_product_id': dutchie_id,
                'location_id': record.x_location_id if hasattr(record, 'x_location_id') else None,
                'changes': {},
            }
            for odoo_field in changed:
                dutchie_field = FIELD_MAP.get(odoo_field)
                if dutchie_field:
                    value = vals[odoo_field]
                    # Convert False/None to empty string for Dutchie
                    if value is False or value is None:
                        value = ''
                    payload['changes'][dutchie_field] = value

            if payload['changes']:
                self._push_to_dutchie_async(payload)

        return res

    def _push_to_dutchie_async(self, payload):
        """Fire-and-forget HTTP POST to inventory service webhook."""
        webhook_url = self.env['ir.config_parameter'].sudo().get_param(
            WEBHOOK_URL_PARAM, DEFAULT_WEBHOOK_URL
        )
        api_key = self.env['ir.config_parameter'].sudo().get_param(
            'mint.inventory_service.api_key', ''
        )

        def _do_post():
            import urllib.request
            try:
                data = json.dumps(payload).encode('utf-8')
                req = urllib.request.Request(
                    webhook_url,
                    data=data,
                    headers={
                        'Content-Type': 'application/json',
                        'X-Api-Key': api_key,
                    },
                    method='POST',
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    _logger.info(
                        'Dutchie sync pushed product %s: %s (HTTP %s)',
                        payload['dutchie_product_id'],
                        list(payload['changes'].keys()),
                        resp.status,
                    )
            except Exception as e:
                _logger.warning(
                    'Dutchie sync push failed for product %s: %s',
                    payload.get('dutchie_product_id'), e,
                )

        thread = threading.Thread(target=_do_post, daemon=True)
        thread.start()


class ProductProduct(models.Model):
    _inherit = 'product.product'

    def write(self, vals):
        res = super().write(vals)

        # Skip automated sync writes
        if self.env.context.get('tracking_disable'):
            return res

        # Skip if no watched fields changed
        changed = WATCHED_FIELDS & set(vals.keys())
        if not changed:
            return res

        for record in self:
            tmpl = record.product_tmpl_id
            dutchie_id = tmpl.x_dutchie_product_id if hasattr(tmpl, 'x_dutchie_product_id') else None
            if not dutchie_id:
                continue

            payload = {
                'model': 'product.product',
                'odoo_id': record.id,
                'template_id': tmpl.id,
                'dutchie_product_id': dutchie_id,
                'location_id': record.x_dutchie_location_id if hasattr(record, 'x_dutchie_location_id') else None,
                'changes': {},
            }
            for odoo_field in changed:
                dutchie_field = FIELD_MAP.get(odoo_field)
                if dutchie_field:
                    value = vals[odoo_field]
                    if value is False or value is None:
                        value = ''
                    payload['changes'][dutchie_field] = value

            if payload['changes']:
                tmpl._push_to_dutchie_async(payload)

        return res
