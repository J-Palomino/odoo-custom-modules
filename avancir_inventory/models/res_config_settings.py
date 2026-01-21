# -*- coding: utf-8 -*-

from odoo import api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # Avancir API Configuration
    avancir_api_url = fields.Char(
        string='Avancir API URL',
        config_parameter='avancir_inventory.api_url',
        default='https://avancir.app/api/v1',
    )
    avancir_username = fields.Char(
        string='Avancir Username',
        config_parameter='avancir_inventory.username',
    )
    avancir_password = fields.Char(
        string='Avancir Password',
        config_parameter='avancir_inventory.password',
    )
    avancir_workspace_key = fields.Char(
        string='Avancir Workspace Key',
        config_parameter='avancir_inventory.workspace_key',
        default='default',
    )
    avancir_item_type = fields.Char(
        string='Default Item Type',
        config_parameter='avancir_inventory.item_type',
        default='product',
        help='The property_name of the Avancir item type for products',
    )
    avancir_default_status = fields.Char(
        string='Default Item Status',
        config_parameter='avancir_inventory.default_status',
        default='Active',
        help='The display_name of the default Avancir status for new items',
    )
    avancir_sync_enabled = fields.Boolean(
        string='Enable Avancir Sync',
        config_parameter='avancir_inventory.sync_enabled',
        default=False,
    )
    avancir_sync_interval = fields.Integer(
        string='Sync Interval (hours)',
        config_parameter='avancir_inventory.sync_interval',
        default=24,
    )

    def action_test_avancir_connection(self):
        """Test the Avancir API connection."""
        self.ensure_one()
        sync_model = self.env['avancir.sync']
        try:
            token = sync_model._get_auth_token()
            if token:
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': 'Success',
                        'message': 'Successfully connected to Avancir API!',
                        'type': 'success',
                        'sticky': False,
                    }
                }
        except Exception as e:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Connection Failed',
                    'message': str(e),
                    'type': 'danger',
                    'sticky': True,
                }
            }

    def action_sync_products_to_avancir(self):
        """Manually trigger product sync to Avancir."""
        self.ensure_one()
        sync_model = self.env['avancir.sync']
        result = sync_model.sync_all_products()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Sync Complete',
                'message': f"Created: {result.get('created', 0)}, Updated: {result.get('updated', 0)}, Errors: {result.get('errors', 0)}",
                'type': 'success' if result.get('errors', 0) == 0 else 'warning',
                'sticky': True,
            }
        }
