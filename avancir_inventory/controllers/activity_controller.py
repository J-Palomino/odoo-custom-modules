# -*- coding: utf-8 -*-

import json
import logging

from odoo import http
from odoo.http import request, Response

_logger = logging.getLogger(__name__)


class AvancirHistoryController(http.Controller):
    """REST API endpoints for fetching RFID activity history from Avancir."""

    def _json_response(self, data, status=200):
        """Return a JSON response."""
        return Response(
            json.dumps(data),
            status=status,
            content_type='application/json',
        )

    def _error_response(self, message, status=400):
        """Return an error response."""
        return self._json_response({
            'success': False,
            'error': message,
        }, status=status)

    def _get_sync_model(self):
        """Get or create an avancir.sync record for API calls."""
        Sync = request.env['avancir.sync'].sudo()
        # Return the model class - we'll create temp records as needed
        return Sync

    # ================================================================
    # HISTORY FETCH ENDPOINTS
    # ================================================================

    @http.route('/api/v1/rfid/history', type='http', auth='public', methods=['GET'], csrf=False)
    def get_history_by_items(self, **kwargs):
        """
        Fetch activity history from Avancir for specific items.

        Query params:
            - item_ids: Comma-separated list of Avancir item IDs (required)

        Returns:
            Activity history from Avancir's /items/history/batch endpoint
        """
        try:
            item_ids_param = kwargs.get('item_ids', '')
            if not item_ids_param:
                return self._error_response('item_ids query parameter is required', 400)

            item_ids = [id.strip() for id in item_ids_param.split(',') if id.strip()]
            if not item_ids:
                return self._error_response('No valid item IDs provided', 400)

            Sync = self._get_sync_model()
            # Create temp sync record for API call
            temp_sync = Sync.create({
                'name': 'History Fetch (temp)',
                'sync_type': 'full',
                'state': 'running',
            })

            try:
                history = temp_sync.get_item_history(item_ids)
                temp_sync.write({'state': 'done'})

                return self._json_response({
                    'success': True,
                    'data': history,
                    'item_count': len(item_ids),
                    'history_count': len(history) if isinstance(history, list) else 0,
                })
            except Exception as e:
                temp_sync.write({'state': 'error', 'error_log': str(e)})
                raise

        except Exception as e:
            _logger.error(f'Error fetching item history: {e}')
            return self._error_response(str(e), 500)

    @http.route('/api/v1/rfid/history/location/<string:location_name>', type='http', auth='public', methods=['GET'], csrf=False)
    def get_history_by_location(self, location_name, **kwargs):
        """
        Fetch activity history for all items at a location.

        Path params:
            - location_name: Name of the location/warehouse

        Query params:
            - limit: Max number of items to fetch history for (default 50)

        Returns:
            Activity history from Avancir for items at that location
        """
        try:
            if not location_name:
                return self._error_response('location_name is required', 400)

            limit = int(kwargs.get('limit', 50))
            if limit < 1:
                limit = 50
            if limit > 200:
                limit = 200

            Sync = self._get_sync_model()
            temp_sync = Sync.create({
                'name': f'History Fetch - {location_name} (temp)',
                'sync_type': 'full',
                'state': 'running',
            })

            try:
                history = temp_sync.get_item_history_by_location(location_name, limit=limit)
                temp_sync.write({'state': 'done'})

                return self._json_response({
                    'success': True,
                    'data': history,
                    'location': location_name,
                    'limit': limit,
                    'history_count': len(history) if isinstance(history, list) else 0,
                })
            except Exception as e:
                temp_sync.write({'state': 'error', 'error_log': str(e)})
                raise

        except Exception as e:
            _logger.error(f'Error fetching history for location {location_name}: {e}')
            return self._error_response(str(e), 500)

    @http.route('/api/v1/rfid/items/<string:item_id>/history', type='http', auth='public', methods=['GET'], csrf=False)
    def get_single_item_history(self, item_id, **kwargs):
        """
        Fetch activity history for a single item.

        Path params:
            - item_id: Avancir item ID

        Returns:
            Activity history for the specified item
        """
        try:
            if not item_id:
                return self._error_response('item_id is required', 400)

            Sync = self._get_sync_model()
            temp_sync = Sync.create({
                'name': f'History Fetch - Item {item_id} (temp)',
                'sync_type': 'full',
                'state': 'running',
            })

            try:
                history = temp_sync.get_item_history_single(item_id)
                temp_sync.write({'state': 'done'})

                return self._json_response({
                    'success': True,
                    'data': history,
                    'item_id': item_id,
                    'history_count': len(history) if isinstance(history, list) else 0,
                })
            except Exception as e:
                temp_sync.write({'state': 'error', 'error_log': str(e)})
                raise

        except Exception as e:
            _logger.error(f'Error fetching history for item {item_id}: {e}')
            return self._error_response(str(e), 500)

    # ================================================================
    # INVENTORY ENDPOINTS (retained for convenience)
    # ================================================================

    @http.route('/api/v1/rfid/inventory/<string:location_name>', type='http', auth='public', methods=['GET'], csrf=False)
    def get_inventory_by_location(self, location_name, **kwargs):
        """
        Get current inventory at a location from Avancir.

        Path params:
            - location_name: Name of the location/warehouse

        Query params:
            - status: Optional status filter

        Returns:
            List of items at the location
        """
        try:
            if not location_name:
                return self._error_response('location_name is required', 400)

            status_filter = kwargs.get('status')

            Sync = self._get_sync_model()
            temp_sync = Sync.create({
                'name': f'Inventory Fetch - {location_name} (temp)',
                'sync_type': 'inventory',
                'state': 'running',
            })

            try:
                items = temp_sync.get_avancir_inventory_by_location(location_name, status_filter=status_filter)
                temp_sync.write({'state': 'done'})

                return self._json_response({
                    'success': True,
                    'data': items,
                    'location': location_name,
                    'item_count': len(items) if isinstance(items, list) else 0,
                })
            except Exception as e:
                temp_sync.write({'state': 'error', 'error_log': str(e)})
                raise

        except Exception as e:
            _logger.error(f'Error fetching inventory for location {location_name}: {e}')
            return self._error_response(str(e), 500)
