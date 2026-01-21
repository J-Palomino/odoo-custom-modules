# -*- coding: utf-8 -*-

from odoo import api, fields, models


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    avancir_item_id = fields.Char(
        string='Avancir Item ID',
        copy=False,
        help='The unique ID of this product in Avancir',
    )
    avancir_last_sync = fields.Datetime(
        string='Last Avancir Sync',
        copy=False,
    )
    avancir_sync_error = fields.Text(
        string='Avancir Sync Error',
        copy=False,
    )

    def action_sync_to_avancir(self):
        """Sync this product to Avancir."""
        self.ensure_one()
        sync_model = self.env['avancir.sync']

        try:
            item = sync_model._map_product_to_avancir_item(self)

            if self.avancir_item_id:
                # Update existing item
                result = sync_model._make_request(
                    'PATCH',
                    f'/items/{self.avancir_item_id}',
                    item
                )
            else:
                # Create new item
                result = sync_model._make_request('POST', '/items', item)
                if result.get('data', {}).get('id'):
                    self.avancir_item_id = result['data']['id']

            self.write({
                'avancir_last_sync': fields.Datetime.now(),
                'avancir_sync_error': False,
            })

            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Success',
                    'message': 'Product synced to Avancir successfully!',
                    'type': 'success',
                    'sticky': False,
                }
            }
        except Exception as e:
            self.avancir_sync_error = str(e)
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Sync Failed',
                    'message': str(e),
                    'type': 'danger',
                    'sticky': True,
                }
            }
