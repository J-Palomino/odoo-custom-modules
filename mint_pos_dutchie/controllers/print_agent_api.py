# -*- coding: utf-8 -*-
"""
HTTP API for the store print-node agent (token-authenticated, no user session).

  POST /mint/print/register  {token, hostname, printers:[{system_name,name?}]}
  POST /mint/print/poll      {token}            -> claims this node's pending jobs
  POST /mint/print/complete  {token, job_id, ok, error?, bytes?}
"""
import json
import logging

from odoo import fields, http
from odoo.http import request

_logger = logging.getLogger(__name__)


class MintPrintAgentApi(http.Controller):

    def _body(self):
        try:
            return json.loads(request.httprequest.data or b'{}')
        except Exception:
            return {}

    def _node(self, token):
        if not token:
            return request.env['mint.print.node']
        return request.env['mint.print.node'].sudo().search(
            [('token', '=', token), ('active', '=', True)], limit=1)

    @http.route('/mint/print/register', type='http', auth='public',
                methods=['POST'], csrf=False)
    def register(self, **kw):
        data = self._body()
        node = self._node(data.get('token'))
        if not node:
            return request.make_json_response({'error': 'bad token'}, status=401)
        node.write({
            'hostname': data.get('hostname') or node.hostname,
            'last_seen': fields.Datetime.now(),
        })
        # upsert printers reported by the agent (by system_name)
        Printer = request.env['mint.print.printer'].sudo()
        existing = {p.system_name: p for p in node.printer_ids}
        for p in (data.get('printers') or []):
            sysname = p.get('system_name')
            if not sysname or sysname in existing:
                continue
            Printer.create({
                'node_id': node.id,
                'system_name': sysname,
                'name': p.get('name') or sysname,
                'role': p.get('role') or 'label',
            })
        printers = [{
            'id': p.id, 'system_name': p.system_name, 'name': p.name,
            'role': p.role, 'is_default': p.is_default,
        } for p in node.printer_ids.filtered('active')]
        return request.make_json_response({
            'node_id': node.id, 'node': node.name,
            'company': node.company_id.name, 'printers': printers,
        })

    @http.route('/mint/print/poll', type='http', auth='public',
                methods=['POST'], csrf=False)
    def poll(self, **kw):
        data = self._body()
        node = self._node(data.get('token'))
        if not node:
            return request.make_json_response({'error': 'bad token'}, status=401)
        node.sudo().write({'last_seen': fields.Datetime.now()})
        Job = request.env['mint.print.job'].sudo()
        jobs = Job.search([('node_id', '=', node.id), ('state', '=', 'pending')],
                          order='create_date', limit=20)
        jobs.write({'state': 'printing'})
        out = [{
            'job_id': j.id,
            'printer': (j.printer_id.system_name or '') if j.printer_id else '',
            'role': j.role,
            'zpl': j.zpl,
        } for j in jobs]
        return request.make_json_response({'jobs': out})

    @http.route('/mint/print/complete', type='http', auth='public',
                methods=['POST'], csrf=False)
    def complete(self, **kw):
        data = self._body()
        node = self._node(data.get('token'))
        if not node:
            return request.make_json_response({'error': 'bad token'}, status=401)
        job = request.env['mint.print.job'].sudo().browse(int(data.get('job_id') or 0))
        if not job.exists() or job.node_id.id != node.id:
            return request.make_json_response({'error': 'job not found'}, status=404)
        if data.get('ok'):
            job.write({'state': 'done', 'bytes': data.get('bytes') or 0,
                       'printed_at': fields.Datetime.now()})
        else:
            job.write({'state': 'error', 'error': (data.get('error') or '')[:255]})
        return request.make_json_response({'ok': True})
