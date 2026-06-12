# -*- coding: utf-8 -*-
"""
Shared helpers for mint_account controllers. Thin facade over
mint_customer_api's JWT infrastructure so we don't duplicate the
secret-management / token-verify logic.
"""
import json
import logging

from odoo.http import request, Response

# Re-export the existing helpers from mint_customer_api — importing at the
# top level is safe because odoo-custom-modules's addons loader guarantees
# dependencies are initialized before this module (per the manifest depends).
from odoo.addons.mint_customer_api.controllers.auth import (
    _verify_and_get_user,
    _get_bearer_token,
    json_response,
    error_response,
)

_logger = logging.getLogger(__name__)


def require_partner():
    """Validate the Authorization header and return the partner record.

    Returns either:
      - (partner_record, None) on success
      - (None, Response) with 401 body when no valid token was presented

    Cart/orders controllers should do:
        partner, err = require_partner()
        if err: return err
        # ... partner is a browse record with sudo ...
    """
    user = _verify_and_get_user()
    if not user:
        return None, error_response('Unauthorized', 401)
    return user.partner_id.sudo(), None


def read_json_body():
    """Parse JSON request body, returning ({}, err_response) on bad input."""
    try:
        data = json.loads(request.httprequest.data or b'{}')
        if not isinstance(data, dict):
            return None, error_response('Body must be a JSON object')
        return data, None
    except (ValueError, TypeError):
        return None, error_response('Invalid JSON body')


def cors_preflight():
    """Return a 204 OPTIONS response for CORS preflight."""
    return Response(
        status=204,
        headers={
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type, Authorization',
            'Access-Control-Max-Age': '86400',
        },
    )
