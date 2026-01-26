# -*- coding: utf-8 -*-

import json
import logging
import requests
from datetime import datetime, timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Module-level token cache (avoid Odoo ORM attribute restrictions)
_avancir_token_cache = {
    'token': None,
    'expiry': None,
}


class AvancirSync(models.Model):
    _name = 'avancir.sync'
    _description = 'Avancir Sync Operations'

    name = fields.Char(string='Sync Name', required=True)
    sync_type = fields.Selection([
        ('products', 'Products'),
        ('inventory', 'Inventory'),
        ('full', 'Full Sync'),
        ('transfer', 'Store Transfer'),
        ('reconciliation', 'POS Reconciliation'),
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

    def _get_config(self, key, default=None):
        """Get configuration parameter."""
        param = self.env['ir.config_parameter'].sudo()
        return param.get_param(f'avancir_inventory.{key}', default)

    def _get_auth_token(self):
        """Get or refresh Avancir session token."""
        global _avancir_token_cache

        # Check if we have a valid cached token
        cached_token = _avancir_token_cache.get('token')
        cached_expiry = _avancir_token_cache.get('expiry')
        if cached_token and cached_expiry and datetime.now() < cached_expiry:
            return cached_token

        api_url = self._get_config('api_url', 'https://avancir.app/api/v1')
        username = self._get_config('username')
        password = self._get_config('password')

        if not username or not password:
            raise UserError('Avancir credentials not configured. Go to Settings > Inventory > Avancir.')

        try:
            response = requests.post(
                f'{api_url}/auth/login',
                json={'identifier': username, 'password': password},
                headers={'Content-Type': 'application/json'},
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()

            # Avancir returns idToken in data.idToken
            token = data.get('data', {}).get('idToken') or data.get('idToken')
            # Token valid for 1 hour, refresh at 50 minutes
            _avancir_token_cache['token'] = token
            _avancir_token_cache['expiry'] = datetime.now() + timedelta(minutes=50)

            return token
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

    # ================================================================
    # INVENTORY TRANSFERS BETWEEN STORES
    # ================================================================

    def transfer_inventory(self, source_warehouse_id, dest_warehouse_id, product_ids, quantities=None):
        """
        Transfer inventory between stores.
        Creates Odoo stock.picking and updates Avancir item locations.

        Args:
            source_warehouse_id: ID of source stock.warehouse
            dest_warehouse_id: ID of destination stock.warehouse
            product_ids: List of product.template IDs to transfer
            quantities: Optional dict {product_id: qty}, defaults to 1 each

        Returns:
            dict with transfer_id, items_transferred, avancir_updated
        """
        if quantities is None:
            quantities = {pid: 1 for pid in product_ids}

        source_wh = self.env['stock.warehouse'].browse(source_warehouse_id)
        dest_wh = self.env['stock.warehouse'].browse(dest_warehouse_id)

        if not source_wh.exists() or not dest_wh.exists():
            raise UserError('Invalid warehouse IDs provided')

        _logger.info(f'Starting inventory transfer: {source_wh.name} -> {dest_wh.name}')

        # Create internal transfer picking
        picking_type = self.env['stock.picking.type'].search([
            ('code', '=', 'internal'),
            ('warehouse_id', '=', source_wh.id),
        ], limit=1)

        if not picking_type:
            # Fallback to any internal transfer type
            picking_type = self.env['stock.picking.type'].search([
                ('code', '=', 'internal'),
            ], limit=1)

        picking_vals = {
            'picking_type_id': picking_type.id,
            'location_id': source_wh.lot_stock_id.id,
            'location_dest_id': dest_wh.lot_stock_id.id,
            'origin': f'Avancir Transfer {datetime.now().strftime("%Y-%m-%d %H:%M")}',
        }

        picking = self.env['stock.picking'].create(picking_vals)
        products = self.env['product.template'].browse(product_ids)
        items_transferred = 0
        avancir_updated = 0
        errors = []

        for product in products:
            qty = quantities.get(product.id, 1)
            # Get product variant
            variant = product.product_variant_id
            if not variant:
                continue

            # Create stock move
            move_vals = {
                'name': product.name,
                'product_id': variant.id,
                'product_uom_qty': qty,
                'product_uom': product.uom_id.id,
                'picking_id': picking.id,
                'location_id': source_wh.lot_stock_id.id,
                'location_dest_id': dest_wh.lot_stock_id.id,
            }
            self.env['stock.move'].create(move_vals)
            items_transferred += 1

            # Update Avancir location
            if product.avancir_item_id:
                try:
                    self._make_request('PATCH', f'/items/{product.avancir_item_id}', {
                        'location': {'display_name': dest_wh.name}
                    })
                    avancir_updated += 1
                except Exception as e:
                    errors.append(f'Avancir update failed for {product.name}: {e}')
                    _logger.error(f'Avancir location update failed: {e}')

        # Confirm the picking
        picking.action_confirm()
        picking.action_assign()

        _logger.info(f'Transfer complete: {items_transferred} items, {avancir_updated} Avancir updates')

        return {
            'transfer_id': picking.id,
            'transfer_name': picking.name,
            'source_warehouse': source_wh.name,
            'dest_warehouse': dest_wh.name,
            'items_transferred': items_transferred,
            'avancir_updated': avancir_updated,
            'errors': errors,
        }

    # ================================================================
    # END-OF-DAY POS RECONCILIATION
    # ================================================================

    def reconcile_pos_inventory(self, warehouse_id, pos_sales_data=None):
        """
        Reconcile end-of-day POS sales with Avancir physical inventory.

        Args:
            warehouse_id: ID of stock.warehouse to reconcile
            pos_sales_data: Optional dict of {sku: qty_sold} from POS
                           If not provided, will attempt to fetch from Avancir

        Returns:
            dict with reconciliation results
        """
        warehouse = self.env['stock.warehouse'].browse(warehouse_id)
        if not warehouse.exists():
            raise UserError('Invalid warehouse ID')

        _logger.info(f'Starting POS reconciliation for {warehouse.name}')

        # Create sync record for tracking
        sync_record = self.create({
            'name': f'POS Reconciliation {warehouse.name} {datetime.now().strftime("%Y-%m-%d")}',
            'sync_type': 'reconciliation',
            'state': 'running',
            'start_time': fields.Datetime.now(),
            'company_id': warehouse.company_id.id,
        })

        # Get current Avancir inventory for this location
        try:
            avancir_items = self._make_request('GET', '/items', params={
                'location': warehouse.name,
                'limit': 1000,
            })
        except Exception as e:
            sync_record.write({'state': 'error', 'error_log': str(e)})
            raise UserError(f'Failed to fetch Avancir inventory: {e}')

        items_data = avancir_items.get('data', [])
        _logger.info(f'Fetched {len(items_data)} items from Avancir for {warehouse.name}')

        # Build Avancir inventory map by SKU
        avancir_by_sku = {}
        for item in items_data:
            sku = item.get('sku')
            if sku:
                avancir_by_sku[sku] = {
                    'avancir_id': item.get('id'),
                    'name': item.get('name'),
                    'status': item.get('status', {}).get('display_name'),
                    'rfid_tag': item.get('rfid_tag'),
                    'last_scan': item.get('last_scanned_at'),
                }

        # Get Odoo products for this warehouse's company
        products = self.env['product.template'].search([
            ('company_id', '=', warehouse.company_id.id),
            ('active', '=', True),
            ('default_code', '!=', False),
        ])

        # Build Odoo inventory map
        odoo_by_sku = {}
        for product in products:
            if product.default_code:
                odoo_by_sku[product.default_code] = {
                    'product_id': product.id,
                    'name': product.name,
                    'qty_available': product.qty_available,
                }

        # Compare and find discrepancies
        discrepancies = []
        matched = 0
        missing_in_avancir = []
        missing_in_odoo = []

        for sku, odoo_data in odoo_by_sku.items():
            if sku in avancir_by_sku:
                matched += 1
                avancir_data = avancir_by_sku[sku]
                # Check if item was sold (status changed or missing RFID scan today)
                if pos_sales_data and sku in pos_sales_data:
                    qty_sold = pos_sales_data[sku]
                    discrepancies.append({
                        'sku': sku,
                        'name': odoo_data['name'],
                        'odoo_qty': odoo_data['qty_available'],
                        'pos_sold': qty_sold,
                        'avancir_status': avancir_data['status'],
                        'last_scan': avancir_data['last_scan'],
                    })
            else:
                missing_in_avancir.append({
                    'sku': sku,
                    'name': odoo_data['name'],
                    'product_id': odoo_data['product_id'],
                })

        for sku, avancir_data in avancir_by_sku.items():
            if sku not in odoo_by_sku:
                missing_in_odoo.append({
                    'sku': sku,
                    'name': avancir_data['name'],
                    'avancir_id': avancir_data['avancir_id'],
                })

        # Update sync record
        sync_record.write({
            'state': 'done',
            'end_time': fields.Datetime.now(),
            'products_created': matched,
            'products_updated': len(discrepancies),
            'errors': len(missing_in_avancir) + len(missing_in_odoo),
            'error_log': json.dumps({
                'missing_in_avancir': missing_in_avancir[:50],
                'missing_in_odoo': missing_in_odoo[:50],
            }, indent=2) if missing_in_avancir or missing_in_odoo else False,
        })

        result = {
            'warehouse': warehouse.name,
            'total_odoo_products': len(odoo_by_sku),
            'total_avancir_items': len(avancir_by_sku),
            'matched': matched,
            'discrepancies': discrepancies,
            'missing_in_avancir': missing_in_avancir,
            'missing_in_odoo': missing_in_odoo,
            'sync_record_id': sync_record.id,
        }

        _logger.info(f'Reconciliation complete: {matched} matched, '
                    f'{len(discrepancies)} discrepancies, '
                    f'{len(missing_in_avancir)} missing in Avancir')

        return result

    def get_avancir_inventory_by_location(self, location_name, status_filter=None):
        """
        Get all Avancir items for a specific location.

        Args:
            location_name: Name of the location/warehouse
            status_filter: Optional status to filter by (e.g., 'Active', 'Needs Tags')

        Returns:
            List of items from Avancir
        """
        params = {
            'location': location_name,
            'limit': 1000,
        }
        if status_filter:
            params['status'] = status_filter

        result = self._make_request('GET', '/items', params=params)
        return result.get('data', [])

    def update_avancir_item_status(self, avancir_item_id, new_status):
        """
        Update the status of an item in Avancir.

        Args:
            avancir_item_id: ID of the item in Avancir
            new_status: New status display name (e.g., 'Sold', 'Transferred', 'Active')

        Returns:
            Updated item data
        """
        return self._make_request('PATCH', f'/items/{avancir_item_id}', {
            'status': {'display_name': new_status}
        })
