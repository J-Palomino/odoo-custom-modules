# -*- coding: utf-8 -*-
"""
Auth endpoints for MintDeals frontend.

Endpoints (all JSON, no session cookies — FE receives JWT and stores it in
an HttpOnly cookie of its own):

  POST /api/v1/auth/register         {email, password, first_name, last_name, phone}
  POST /api/v1/auth/login            {email, password}
  POST /api/v1/auth/forgot-password  {email}
  POST /api/v1/auth/reset-password   {token, new_password}
  GET  /api/v1/auth/me               Authorization: Bearer <jwt>

Fleshed out in Task #4. This stub exists so the module installs cleanly
and the routes resolve to a 501 (rather than 404) while the build is
still in flight.
"""
from odoo import http
from odoo.http import Response


@http.route(
    ['/api/v1/auth/register', '/api/v1/auth/login',
     '/api/v1/auth/forgot-password', '/api/v1/auth/reset-password'],
    type='http', auth='public', methods=['POST'], csrf=False, cors='*',
)
def auth_stub(**kw):
    return Response(
        '{"error":"not implemented"}',
        status=501, content_type='application/json',
    )


@http.route(
    '/api/v1/auth/me',
    type='http', auth='public', methods=['GET'], csrf=False, cors='*',
)
def auth_me_stub(**kw):
    return Response(
        '{"error":"not implemented"}',
        status=501, content_type='application/json',
    )
