# -*- coding: utf-8 -*-

import json
import logging
import requests
from datetime import datetime, timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class AvancirSync(models.Model):
    _name = 'avancir.sync'
    _description = 'Avancir Sync Operations'

    name = fields.Char(string='Sync Name', required=True)
    sync_type = fields.Selection([
        ('products', 'Products'),
        ('inventory', 'Inventory'),
        ('full', 'Full Sync'),
    ], string='Sync Type', default='products')
    state = fields.Selection([
        ('draft', 'Draft'),
        ('running', 'Running'),
        ('done', 'Done'),
        ('error', 'Error'),
    ], string='State', default='draft')
    start_time = fields.Datetime(string='Start Time')
    end_time = fields.Datetime(string='End Time')
    products_created = fields.Integer(string='Products Created', default=0)
    products_updated = fields.Integer(string='Products Updated', default=0)
    errors = fields.Integer(string='Errors', default=0)
    error_log = fields.Text(string='Error Log')
    company_id = fields.Many2one('res.company', string='Company')

    # Session token cache
    _session_token = None
    _token_expiry = None

    def _get_config(self, key, default=None):
        """Get configuration parameter."""
        param = self.env['ir.config_parameter'].sudo()
        return param.get_param(f'avancir_inventory.{key}', default)

    def _get_auth_token(self):
        """Get or refresh Avancir session token."""
        # Check if we have a valid cached token
        if self._session_token and self._token_expiry and datetime.now() < self._token_expiry:
            return self._session_token

        api_url = self._get_config('api_url', 'https://avancir.app/api/v1')
        username = self._get_config('username')
        password = self._get_config('password')

        if not username or not password:
            raise UserError('Avancir credentials not configured. Go to Settings > Inventory > Avancir.')

        try:
            response = requests.post(
                f'{api_url}/auth/login',
                json={'email': username, 'password': password},
                headers={'Content-Type': 'application/json'},
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()

            self._session_token = data.get('data', {}).get('sessionToken') or data.get('sessionToken')
            # Token valid for 1 hour, refresh at 50 minutes
            self._token_expiry = datetime.now() + timedelta(minutes=50)

            return self._session_token
        except requests.exceptions.RequestException as e:
            _logger.error(f'Avancir auth failed: {e}')
            raise UserError(f'Failed to authenticate with Avancir: {e}')

    def _make_request(self, method, endpoint, data=None, params=None):
        """Make authenticated request to Avancir API."""
        api_url = self._get_config('api_url', 'https://avancir.app/api/v1')
        workspace_key = self._get_config('workspace_key', 'default')
        token = self._get_auth_token()

        headers = {
            'Content-Type': 'application/json',
            'x-session-token': token,
        }

        # Add workspace key to params
        if params is None:
            params = {}
        params['workspaceKey'] = workspace_key

        url = f'{api_url}{endpoint}'

        try:
            if method.upper() == 'GET':
                response = requests.get(url, headers=headers, params=params, timeout=60)
            elif method.upper() == 'POST':
                response = requests.post(url, headers=headers, json=data, params=params, timeout=60)
            elif method.upper() == 'PATCH':
                response = requests.patch(url, headers=headers, json=data, params=params, timeout=60)
            else:
                raise ValueError(f'Unsupported HTTP method: {method}')

            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            _logger.error(f'Avancir API request failed: {e}')
            raise

    def _map_product_to_avancir_item(self, product):
        """Map Odoo product to Avancir item format."""
        item_type = self._get_config('item_type', 'product')
        default_status = self._get_config('default_status', 'Active')

        # Get location from warehouse if available
        location_name = None
        if product.company_id:
            warehouse = self.env['stock.warehouse'].search([
                ('company_id', '=', product.company_id.id)
            ], limit=1)
            if warehouse:
                location_name = warehouse.name

        item = {
            'name': product.name,
            'type': {'property_name': item_type},
            'status': {'display_name': default_status},
        }

        if location_name:
            item['location'] = {'display_name': location_name}

        # Map custom fields
        if product.default_code:
            item['sku'] = product.default_code
        if product.list_price:
            item['price'] = product.list_price
        if product.x_brand:
            item['brand'] = product.x_brand
        if product.x_strain_type:
            item['strain_type'] = product.x_strain_type
        if product.x_strain:
            item['strain'] = product.x_strain
        if product.x_thc:
            item['thc'] = product.x_thc
        if product.x_cbd:
            item['cbd'] = product.x_cbd
        if product.x_image_url:
            item['image_url'] = product.x_image_url
        if product.categ_id:
            item['category'] = product.categ_id.name

        # Store Odoo product ID for reference
        item['odoo_product_id'] = str(product.id)

        return item

    def sync_all_products(self, company_id=None, batch_size=100):
        """Sync all products to Avancir using bulk API."""
        sync_enabled = self._get_config('sync_enabled', False)
        if not sync_enabled:
            _logger.info('Avancir sync is disabled')
            return {'created': 0, 'updated': 0, 'errors': 0}

        # Create sync record
        sync_record = self.create({
            'name': f'Product Sync {datetime.now().strftime("%Y-%m-%d %H:%M")}',
            'sync_type': 'products',
            'state': 'running',
            'start_time': fields.Datetime.now(),
            'company_id': company_id,
        })

        domain = [('active', '=', True), ('sale_ok', '=', True)]
        if company_id:
            domain.append(('company_id', '=', company_id))

        products = self.env['product.template'].search(domain)
        total = len(products)
        created = 0
        updated = 0
        errors = 0
        error_messages = []

        _logger.info(f'Starting Avancir sync for {total} products')

        # Process in batches
        for i in range(0, total, batch_size):
            batch = products[i:i + batch_size]
            items = []

            for product in batch:
                try:
                    item = self._map_product_to_avancir_item(product)
                    items.append(item)
                except Exception as e:
                    errors += 1
                    error_messages.append(f'Product {product.id}: {e}')
                    _logger.error(f'Error mapping product {product.id}: {e}')

            if items:
                try:
                    # Use bulk create endpoint
                    result = self._make_request('POST', '/items/bulkCreate', {'items': items})
                    batch_created = len(result.get('data', []))
                    created += batch_created
                    _logger.info(f'Batch {i // batch_size + 1}: Created {batch_created} items')
                except Exception as e:
                    errors += len(items)
                    error_messages.append(f'Batch {i // batch_size + 1}: {e}')
                    _logger.error(f'Bulk create failed: {e}')

            # Update sync record progress
            sync_record.write({
                'products_created': created,
                'products_updated': updated,
                'errors': errors,
            })

        # Finalize sync record
        sync_record.write({
            'state': 'done' if errors == 0 else 'error',
            'end_time': fields.Datetime.now(),
            'error_log': '\n'.join(error_messages) if error_messages else False,
        })

        _logger.info(f'Avancir sync complete: {created} created, {updated} updated, {errors} errors')

        return {
            'created': created,
            'updated': updated,
            'errors': errors,
        }

    @api.model
    def cron_sync_products(self):
        """Cron job to sync products to Avancir."""
        _logger.info('Running scheduled Avancir product sync')
        return self.sync_all_products()
