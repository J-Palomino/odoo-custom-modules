# -*- coding: utf-8 -*-
"""
Auto-configure products for Odoo POS.

Maps master_category → pos_categ_id and ensures cannabis products
are available in the POS interface with proper barcodes.
"""
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# master_category value → XML ID of pos.category record
CATEGORY_MAP = {
    'flower': 'mint_pos_dutchie.pos_categ_flower',
    'prerolls': 'mint_pos_dutchie.pos_categ_prerolls',
    'vaporizers': 'mint_pos_dutchie.pos_categ_vaporizers',
    'concentrates': 'mint_pos_dutchie.pos_categ_concentrates',
    'edibles': 'mint_pos_dutchie.pos_categ_edibles',
    'tinctures': 'mint_pos_dutchie.pos_categ_tinctures',
    'topicals': 'mint_pos_dutchie.pos_categ_topicals',
    'beverages': 'mint_pos_dutchie.pos_categ_beverages',
    'accessories': 'mint_pos_dutchie.pos_categ_accessories',
}


class ProductTemplatePosConfig(models.Model):
    _inherit = 'product.template'

    available_in_pos = fields.Boolean(default=True)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._sync_pos_category()
        records._sync_barcode_from_sku()
        return records

    def write(self, vals):
        res = super().write(vals)
        if 'master_category' in vals:
            self._sync_pos_category()
        if 'default_code' in vals and not vals.get('barcode'):
            self._sync_barcode_from_sku()
        return res

    def _sync_pos_category(self):
        """Set pos_categ_id from master_category if not already set."""
        for product in self:
            if product.pos_categ_ids or not product.master_category:
                continue
            xml_id = CATEGORY_MAP.get(product.master_category)
            if not xml_id:
                continue
            try:
                categ = self.env.ref(xml_id, raise_if_not_found=False)
                if categ:
                    product.pos_categ_ids = [(4, categ.id)]
            except Exception:
                _logger.debug(
                    'POS category %s not found for product %s',
                    xml_id, product.id,
                )

    def _sync_barcode_from_sku(self):
        """Populate barcode from default_code (Dutchie SKU) if missing."""
        for product in self:
            if not product.barcode and product.default_code:
                product.barcode = product.default_code
