# -*- coding: utf-8 -*-
"""
MintDeals REST API v2 - Using Odoo native HTTP controllers.

All endpoints return JSON responses and are accessible at /api/v1/
"""
import json
import logging
from datetime import datetime

from odoo import http
from odoo.http import request, Response

_logger = logging.getLogger(__name__)


def json_response(data, status=200):
    """Helper to create JSON responses with proper headers."""
    return Response(
        json.dumps(data, default=str),
        status=status,
        content_type='application/json',
        headers={
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type, Authorization',
        }
    )


def error_response(message, status=400):
    """Helper to create error responses."""
    return json_response({'error': message, 'status': status}, status=status)


class MintDealsAPI(http.Controller):
    """REST API Controller for MintDeals frontend."""

    # ==================== HEALTH ====================

    @http.route('/api/v1/health', type='http', auth='none', methods=['GET'], csrf=False, cors='*')
    def health_check(self):
        """Health check endpoint."""
        return json_response({
            'status': 'healthy',
            'timestamp': datetime.utcnow().isoformat(),
            'version': '2.0.0',
            'service': 'mint-api',
        })

    # ==================== STORES ====================

    def _company_to_dict(self, company):
        """Convert res.company record to dictionary."""
        hours_dict = company.get_hours_dict() if hasattr(company, 'get_hours_dict') else {}

        return {
            'id': company.id,
            'name': company.name,
            'slug': getattr(company, 'slug', None),
            'store_code': getattr(company, 'store_code', None),
            'phone': company.phone,
            'email': company.email,
            'timezone': company.partner_id.tz if company.partner_id else None,
            'dutchie_store_id': getattr(company, 'dutchie_store_id', None),
            'menu_url': getattr(company, 'menu_url', None),
            'online_ordering_url': getattr(company, 'online_ordering_url', None),
            'is_medical': getattr(company, 'is_medical', False),
            'is_recreational': getattr(company, 'is_recreational', True),
            'has_cafe': getattr(company, 'has_cafe', False),
            'is_24hours': getattr(company, 'is_24hours', False),
            'license_number': getattr(company, 'license_number', None),
            'geo': {
                'lat': getattr(company, 'latitude', None),
                'lng': getattr(company, 'longitude', None),
            } if getattr(company, 'latitude', None) else None,
            'street': company.street,
            'street2': company.street2,
            'city': company.city,
            'state': company.state_id.name if company.state_id else None,
            'zip': company.zip,
            'country': company.country_id.name if company.country_id else None,
            'google_place_id': getattr(company, 'google_place_id', None),
            'google_map_embed': getattr(company, 'google_map_embed', None),
            'google_map_link': getattr(company, 'google_map_link', None),
            'summary': getattr(company, 'summary', None),
            'about': getattr(company, 'about', None),
            'description': getattr(company, 'description', None),
            'tickertape': getattr(company, 'tickertape', None),
            'image_url': f"/web/image/res.company/{company.id}/logo" if company.logo else None,
            'hero_image_url': getattr(company, 'hero_image_url', None) or (
                f"/web/image/res.company/{company.id}/hero_image" if getattr(company, 'hero_image', None) else None
            ),
            'hours': hours_dict if hours_dict else None,
            'region': {
                'id': company.region_id.id,
                'name': company.region_id.name,
            } if getattr(company, 'region_id', None) else None,
            'amenities': [
                {'id': a.id, 'name': a.name, 'icon': a.icon}
                for a in getattr(company, 'amenity_ids', [])
            ],
            'services': [
                {'id': s.id, 'name': s.name, 'icon': s.icon}
                for s in getattr(company, 'service_ids', [])
            ],
        }

    @http.route('/api/v1/stores', type='http', auth='none', methods=['GET'], csrf=False, cors='*')
    def get_stores(self, **kwargs):
        """Get all dispensary stores."""
        try:
            domain = [('is_dispensary', '=', True)]

            # Filter by active status
            is_active = kwargs.get('is_active', 'true').lower() == 'true'
            domain.append(('is_active', '=', is_active))

            # Filter by region
            region_id = kwargs.get('region_id')
            if region_id:
                domain.append(('region_id', '=', int(region_id)))

            limit = int(kwargs.get('limit', 100))
            offset = int(kwargs.get('offset', 0))

            Company = request.env["res.company"].sudo()
            total_count = Company.search_count(domain)
            companies = Company.search(domain, limit=limit, offset=offset, order="name asc")

            return json_response({
                'count': total_count,
                'items': [self._company_to_dict(c) for c in companies],
            })
        except Exception as e:
            _logger.error("Error getting stores: %s", e)
            return error_response(str(e), 500)

    @http.route('/api/v1/stores/<int:store_id>', type='http', auth='none', methods=['GET'], csrf=False, cors='*')
    def get_store_by_id(self, store_id):
        """Get a store by ID."""
        try:
            company = request.env["res.company"].sudo().browse(store_id)

            if not company.exists() or not getattr(company, 'is_dispensary', False):
                return error_response("Store not found", 404)

            return json_response(self._company_to_dict(company))
        except Exception as e:
            _logger.error("Error getting store %s: %s", store_id, e)
            return error_response(str(e), 500)

    @http.route('/api/v1/stores/slug/<string:slug>', type='http', auth='none', methods=['GET'], csrf=False, cors='*')
    def get_store_by_slug(self, slug):
        """Get a store by URL slug."""
        try:
            company = request.env["res.company"].sudo().search([
                ('slug', '=', slug),
                ('is_dispensary', '=', True),
            ], limit=1)

            if not company:
                return error_response("Store not found", 404)

            return json_response(self._company_to_dict(company))
        except Exception as e:
            _logger.error("Error getting store by slug %s: %s", slug, e)
            return error_response(str(e), 500)

    @http.route('/api/v1/stores/dutchie/<string:dutchie_id>', type='http', auth='none', methods=['GET'], csrf=False, cors='*')
    def get_store_by_dutchie_id(self, dutchie_id):
        """Get a store by Dutchie Store ID."""
        try:
            company = request.env["res.company"].sudo().search([
                ('dutchie_store_id', '=', dutchie_id),
                ('is_dispensary', '=', True),
            ], limit=1)

            if not company:
                return error_response("Store not found", 404)

            return json_response(self._company_to_dict(company))
        except Exception as e:
            _logger.error("Error getting store by dutchie_id %s: %s", dutchie_id, e)
            return error_response(str(e), 500)

    # ==================== PRODUCTS ====================

    def _product_to_dict(self, product, store_id=None):
        """Convert product.template record to dictionary."""
        return {
            'id': product.id,
            'name': product.name,
            'sku': product.default_code,
            'barcode': product.barcode,
            'category': product.categ_id.name if product.categ_id else None,
            'category_id': product.categ_id.id if product.categ_id else None,
            'description': product.description_sale,
            'price': product.list_price,
            'cost': product.standard_price,
            'weight': product.weight,
            'weight_unit': getattr(product, 'weight_unit', 'g'),
            'brand': getattr(product, 'brand', None),
            'strain_type': getattr(product, 'strain_type', None),
            'thc_percentage': getattr(product, 'thc_percentage', None),
            'cbd_percentage': getattr(product, 'cbd_percentage', None),
            'effects': getattr(product, 'effects', None),
            'flavors': getattr(product, 'flavors', None),
            'image_url': f"/web/image/product.template/{product.id}/image_1920" if product.image_1920 else None,
            'active': product.active,
            'dutchie_product_id': getattr(product, 'dutchie_product_id', None),
            'available_quantity': product.qty_available,
            'store_id': store_id,
        }

    @http.route('/api/v1/products', type='http', auth='none', methods=['GET'], csrf=False, cors='*')
    def get_products(self, **kwargs):
        """Get products with optional filters."""
        try:
            domain = [('sale_ok', '=', True), ('active', '=', True)]

            # Filter by category
            category_id = kwargs.get('category_id')
            if category_id:
                domain.append(('categ_id', '=', int(category_id)))

            # Filter by store/company
            store_id = kwargs.get('store_id')
            if store_id:
                domain.append(('company_id', '=', int(store_id)))

            # Search by name
            search = kwargs.get('search')
            if search:
                domain.append(('name', 'ilike', search))

            limit = int(kwargs.get('limit', 100))
            offset = int(kwargs.get('offset', 0))

            Product = request.env["product.template"].sudo()
            total_count = Product.search_count(domain)
            products = Product.search(domain, limit=limit, offset=offset, order="name asc")

            return json_response({
                'count': total_count,
                'items': [self._product_to_dict(p, store_id) for p in products],
            })
        except Exception as e:
            _logger.error("Error getting products: %s", e)
            return error_response(str(e), 500)

    @http.route('/api/v1/products/<int:product_id>', type='http', auth='none', methods=['GET'], csrf=False, cors='*')
    def get_product_by_id(self, product_id):
        """Get a product by ID."""
        try:
            product = request.env["product.template"].sudo().browse(product_id)

            if not product.exists():
                return error_response("Product not found", 404)

            return json_response(self._product_to_dict(product))
        except Exception as e:
            _logger.error("Error getting product %s: %s", product_id, e)
            return error_response(str(e), 500)

    @http.route('/api/v1/stores/<int:store_id>/products', type='http', auth='none', methods=['GET'], csrf=False, cors='*')
    def get_store_products(self, store_id, **kwargs):
        """Get products for a specific store."""
        try:
            domain = [
                ('sale_ok', '=', True),
                ('active', '=', True),
                ('company_id', '=', store_id),
            ]

            category_id = kwargs.get('category_id')
            if category_id:
                domain.append(('categ_id', '=', int(category_id)))

            search = kwargs.get('search')
            if search:
                domain.append(('name', 'ilike', search))

            limit = int(kwargs.get('limit', 100))
            offset = int(kwargs.get('offset', 0))

            Product = request.env["product.template"].sudo()
            total_count = Product.search_count(domain)
            products = Product.search(domain, limit=limit, offset=offset, order="name asc")

            return json_response({
                'count': total_count,
                'items': [self._product_to_dict(p, store_id) for p in products],
            })
        except Exception as e:
            _logger.error("Error getting store products: %s", e)
            return error_response(str(e), 500)

    # ==================== DISCOUNTS ====================

    def _discount_to_dict(self, discount):
        """Convert mint.discount record to dictionary."""
        return {
            'id': discount.id,
            'name': discount.name,
            'slug': getattr(discount, 'slug', None),
            'discount_type': discount.discount_type,
            'discount_amount': discount.discount_amount,
            'discount_percent': discount.discount_percent,
            'description': discount.description,
            'terms': getattr(discount, 'terms', None),
            'valid_from': discount.valid_from.isoformat() if discount.valid_from else None,
            'valid_until': discount.valid_until.isoformat() if discount.valid_until else None,
            'is_active': discount.is_active,
            'is_featured': getattr(discount, 'is_featured', False),
            'store_ids': discount.store_ids.ids if discount.store_ids else [],
            'product_ids': discount.product_ids.ids if discount.product_ids else [],
            'category_ids': discount.category_ids.ids if discount.category_ids else [],
            'image_url': f"/web/image/mint.discount/{discount.id}/image" if getattr(discount, 'image', None) else None,
            'dutchie_discount_id': getattr(discount, 'dutchie_discount_id', None),
        }

    @http.route('/api/v1/discounts', type='http', auth='none', methods=['GET'], csrf=False, cors='*')
    def get_discounts(self, **kwargs):
        """Get active discounts."""
        try:
            today = datetime.now().date()
            domain = [
                ('is_active', '=', True),
                '|',
                ('valid_from', '=', False),
                ('valid_from', '<=', today),
                '|',
                ('valid_until', '=', False),
                ('valid_until', '>=', today),
            ]

            store_id = kwargs.get('store_id')
            if store_id:
                domain.append(('store_ids', 'in', [int(store_id)]))

            featured_only = kwargs.get('featured', '').lower() == 'true'
            if featured_only:
                domain.append(('is_featured', '=', True))

            limit = int(kwargs.get('limit', 100))
            offset = int(kwargs.get('offset', 0))

            Discount = request.env["mint.discount"].sudo()
            total_count = Discount.search_count(domain)
            discounts = Discount.search(domain, limit=limit, offset=offset, order="valid_until asc")

            return json_response({
                'count': total_count,
                'items': [self._discount_to_dict(d) for d in discounts],
            })
        except Exception as e:
            _logger.error("Error getting discounts: %s", e)
            return error_response(str(e), 500)

    @http.route('/api/v1/stores/<int:store_id>/discounts', type='http', auth='none', methods=['GET'], csrf=False, cors='*')
    def get_store_discounts(self, store_id, **kwargs):
        """Get discounts for a specific store."""
        try:
            today = datetime.now().date()
            domain = [
                ('is_active', '=', True),
                ('store_ids', 'in', [store_id]),
                '|',
                ('valid_from', '=', False),
                ('valid_from', '<=', today),
                '|',
                ('valid_until', '=', False),
                ('valid_until', '>=', today),
            ]

            limit = int(kwargs.get('limit', 100))
            offset = int(kwargs.get('offset', 0))

            Discount = request.env["mint.discount"].sudo()
            total_count = Discount.search_count(domain)
            discounts = Discount.search(domain, limit=limit, offset=offset, order="valid_until asc")

            return json_response({
                'count': total_count,
                'items': [self._discount_to_dict(d) for d in discounts],
            })
        except Exception as e:
            _logger.error("Error getting store discounts: %s", e)
            return error_response(str(e), 500)

    # ==================== BLOG ====================

    def _blog_to_dict(self, blog):
        """Convert mint.blog record to dictionary."""
        return {
            'id': blog.id,
            'title': blog.title,
            'slug': blog.slug,
            'excerpt': blog.excerpt,
            'content': blog.content,
            'author': blog.author_id.name if blog.author_id else None,
            'published_at': blog.published_at.isoformat() if blog.published_at else None,
            'is_published': blog.is_published,
            'is_featured': getattr(blog, 'is_featured', False),
            'category': blog.category_id.name if blog.category_id else None,
            'tags': [{'id': t.id, 'name': t.name} for t in blog.tag_ids] if blog.tag_ids else [],
            'image_url': f"/web/image/mint.blog/{blog.id}/image" if blog.image else None,
            'read_time': getattr(blog, 'read_time', None),
        }

    @http.route('/api/v1/blog', type='http', auth='none', methods=['GET'], csrf=False, cors='*')
    def get_blog_posts(self, **kwargs):
        """Get published blog posts."""
        try:
            domain = [('is_published', '=', True)]

            category_id = kwargs.get('category_id')
            if category_id:
                domain.append(('category_id', '=', int(category_id)))

            featured_only = kwargs.get('featured', '').lower() == 'true'
            if featured_only:
                domain.append(('is_featured', '=', True))

            limit = int(kwargs.get('limit', 20))
            offset = int(kwargs.get('offset', 0))

            Blog = request.env["mint.blog"].sudo()
            total_count = Blog.search_count(domain)
            posts = Blog.search(domain, limit=limit, offset=offset, order="published_at desc")

            return json_response({
                'count': total_count,
                'items': [self._blog_to_dict(p) for p in posts],
            })
        except Exception as e:
            _logger.error("Error getting blog posts: %s", e)
            return error_response(str(e), 500)

    @http.route('/api/v1/blog/<string:slug>', type='http', auth='none', methods=['GET'], csrf=False, cors='*')
    def get_blog_post_by_slug(self, slug):
        """Get a blog post by slug."""
        try:
            post = request.env["mint.blog"].sudo().search([
                ('slug', '=', slug),
                ('is_published', '=', True),
            ], limit=1)

            if not post:
                return error_response("Blog post not found", 404)

            return json_response(self._blog_to_dict(post))
        except Exception as e:
            _logger.error("Error getting blog post %s: %s", slug, e)
            return error_response(str(e), 500)

    # ==================== EVENTS ====================

    def _event_to_dict(self, event):
        """Convert mint.event record to dictionary."""
        return {
            'id': event.id,
            'title': event.title,
            'slug': event.slug,
            'description': event.description,
            'event_type': event.event_type,
            'start_datetime': event.start_datetime.isoformat() if event.start_datetime else None,
            'end_datetime': event.end_datetime.isoformat() if event.end_datetime else None,
            'is_all_day': event.is_all_day,
            'location': event.location,
            'store_id': event.store_id.id if event.store_id else None,
            'store_name': event.store_id.name if event.store_id else None,
            'is_recurring': getattr(event, 'is_recurring', False),
            'recurrence_rule': getattr(event, 'recurrence_rule', None),
            'is_published': event.is_published,
            'image_url': f"/web/image/mint.event/{event.id}/image" if event.image else None,
        }

    @http.route('/api/v1/events', type='http', auth='none', methods=['GET'], csrf=False, cors='*')
    def get_events(self, **kwargs):
        """Get upcoming events."""
        try:
            now = datetime.now()
            domain = [
                ('is_published', '=', True),
                ('end_datetime', '>=', now),
            ]

            store_id = kwargs.get('store_id')
            if store_id:
                domain.append(('store_id', '=', int(store_id)))

            event_type = kwargs.get('event_type')
            if event_type:
                domain.append(('event_type', '=', event_type))

            limit = int(kwargs.get('limit', 20))
            offset = int(kwargs.get('offset', 0))

            Event = request.env["mint.event"].sudo()
            total_count = Event.search_count(domain)
            events = Event.search(domain, limit=limit, offset=offset, order="start_datetime asc")

            return json_response({
                'count': total_count,
                'items': [self._event_to_dict(e) for e in events],
            })
        except Exception as e:
            _logger.error("Error getting events: %s", e)
            return error_response(str(e), 500)

    @http.route('/api/v1/stores/<int:store_id>/events', type='http', auth='none', methods=['GET'], csrf=False, cors='*')
    def get_store_events(self, store_id, **kwargs):
        """Get events for a specific store."""
        try:
            now = datetime.now()
            domain = [
                ('is_published', '=', True),
                ('store_id', '=', store_id),
                ('end_datetime', '>=', now),
            ]

            limit = int(kwargs.get('limit', 20))
            offset = int(kwargs.get('offset', 0))

            Event = request.env["mint.event"].sudo()
            total_count = Event.search_count(domain)
            events = Event.search(domain, limit=limit, offset=offset, order="start_datetime asc")

            return json_response({
                'count': total_count,
                'items': [self._event_to_dict(e) for e in events],
            })
        except Exception as e:
            _logger.error("Error getting store events: %s", e)
            return error_response(str(e), 500)

    # ==================== CATEGORIES ====================

    @http.route('/api/v1/categories', type='http', auth='none', methods=['GET'], csrf=False, cors='*')
    def get_categories(self, **kwargs):
        """Get product categories."""
        try:
            domain = []

            parent_id = kwargs.get('parent_id')
            if parent_id == 'null' or parent_id == '0':
                domain.append(('parent_id', '=', False))
            elif parent_id:
                domain.append(('parent_id', '=', int(parent_id)))

            Category = request.env["product.category"].sudo()
            categories = Category.search(domain, order="name asc")

            return json_response({
                'count': len(categories),
                'items': [
                    {
                        'id': c.id,
                        'name': c.name,
                        'parent_id': c.parent_id.id if c.parent_id else None,
                        'parent_name': c.parent_id.name if c.parent_id else None,
                        'complete_name': c.complete_name,
                    }
                    for c in categories
                ],
            })
        except Exception as e:
            _logger.error("Error getting categories: %s", e)
            return error_response(str(e), 500)
