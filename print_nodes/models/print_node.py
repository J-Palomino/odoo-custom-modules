# -*- coding: utf-8 -*-
"""
Self-hosted print network — a store "print node" + its printers + a job queue.

Mimics PrintNode's model without the SaaS so that ANY device at a store can
print to a printer physically connected to that store's node:

  POS device (any)  --enqueue job-->  Odoo queue  <--poll--  store node agent
                                                              --> OS printer

Each store (res.company) has one or more nodes. A node is a machine running
mint_zebra_agent.py in node mode; it registers itself + the printers it sees,
polls Odoo for that store's pending jobs, prints them, and reports back.

On-node printing can skip the queue and go straight to the agent's localhost
endpoint (the "hybrid" fast path); other devices route through this queue.
"""
import secrets

from odoo import api, fields, models


class MintPrintNode(models.Model):
    _name = 'print.node'
    _description = 'Store Print Node'
    _order = 'company_id, name'

    name = fields.Char(required=True, default='New Node')
    company_id = fields.Many2one(
        'res.company', string='Store', required=True,
        default=lambda self: self.env.company,
        help='The store this node prints for.')
    token = fields.Char(
        string='Agent Token', required=True, copy=False,
        default=lambda self: secrets.token_urlsafe(24),
        groups='point_of_sale.group_pos_manager',
        help='Secret the local agent uses to register/poll. Keep private. '
             'Manager-only: a cashier who can read this can poll and complete '
             'that store\'s print queue. The agent controller reads it with '
             'sudo(), so restricting it here does not affect agent auth.')
    hostname = fields.Char(string='Machine', readonly=True)
    agent_url = fields.Char(
        string='Localhost Agent URL', default='http://127.0.0.1:17777',
        help='Used by the on-node fast path (this machine prints directly).')
    last_seen = fields.Datetime(readonly=True)
    online = fields.Boolean(compute='_compute_online')
    active = fields.Boolean(default=True)
    printer_ids = fields.One2many('print.printer', 'node_id', string='Printers')
    job_count = fields.Integer(compute='_compute_job_count')

    @api.depends('last_seen')
    def _compute_online(self):
        cutoff = fields.Datetime.subtract(fields.Datetime.now(), minutes=3)
        for n in self:
            n.online = bool(n.last_seen and n.last_seen >= cutoff)

    def _compute_job_count(self):
        data = self.env['print.job']._read_group(
            [('node_id', 'in', self.ids), ('state', '=', 'pending')],
            ['node_id'], ['__count'])
        counts = {n.id: c for n, c in data}
        for n in self:
            n.job_count = counts.get(n.id, 0)

    def action_rotate_token(self):
        for n in self:
            n.token = secrets.token_urlsafe(24)


class MintPrintPrinter(models.Model):
    _name = 'print.printer'
    _description = 'Print Node Printer'
    _order = 'node_id, role, name'

    name = fields.Char(required=True)
    node_id = fields.Many2one('print.node', required=True, ondelete='cascade')
    company_id = fields.Many2one(related='node_id.company_id', store=True)
    system_name = fields.Char(
        string='OS Printer Name', required=True,
        help='The printer name as the OS/agent sees it.')
    role = fields.Selection(
        [('label', 'Label (ZD410)'), ('receipt', 'Receipt'), ('other', 'Other')],
        default='label', required=True,
        help='Used for default routing: label jobs -> the default label '
             'printer, receipt jobs -> the default receipt printer.')
    is_default = fields.Boolean(
        string='Default for role',
        help='Default printer for its role on this node.')
    active = fields.Boolean(default=True)

    @api.onchange('is_default')
    def _onchange_default(self):
        # only one default per (node, role)
        if self.is_default and self.node_id:
            others = self.node_id.printer_ids.filtered(
                lambda p: p.role == self.role and p.id != self._origin.id)
            others.is_default = False


class MintPrintJob(models.Model):
    _name = 'print.job'
    _description = 'Print Job'
    _order = 'create_date desc'

    name = fields.Char(default='Print Job')
    node_id = fields.Many2one('print.node', required=True, ondelete='cascade')
    company_id = fields.Many2one(related='node_id.company_id', store=True)
    printer_id = fields.Many2one('print.printer', ondelete='set null',
                                 help='Resolved target printer.')
    role = fields.Selection(
        [('label', 'Label'), ('receipt', 'Receipt'), ('other', 'Other')],
        default='label')
    doc_type = fields.Selection(
        [('zpl', 'ZPL / raw'), ('pdf', 'PDF (OS driver)')],
        default='zpl', required=True,
        help='ZPL/raw bytes go straight to the printer; PDF is rendered by the '
             'OS print driver (works on non-Zebra printers).')
    zpl = fields.Text(help='Raw ZPL / printer commands (for doc_type=zpl).')
    pdf_data = fields.Binary(string='PDF', attachment=True,
                             help='Base64 PDF (for doc_type=pdf).')
    state = fields.Selection(
        [('pending', 'Pending'), ('printing', 'Printing'),
         ('done', 'Done'), ('error', 'Error'), ('cancelled', 'Cancelled')],
        default='pending', index=True)
    error = fields.Char()
    bytes = fields.Integer()
    printed_at = fields.Datetime()
    source = fields.Char(help='pos order ref / designer / etc.')

    def action_cancel(self):
        self.filtered(lambda j: j.state in ('pending', 'printing')).write(
            {'state': 'cancelled'})

    def action_retry(self):
        self.filtered(lambda j: j.state in ('error', 'cancelled')).write(
            {'state': 'pending', 'error': False})

    # ── routing helpers (used by the POS) ───────────────────────────
    @api.model
    def _node_for_company(self, company_id):
        return self.env['print.node'].search(
            [('company_id', '=', company_id), ('active', '=', True)],
            order='last_seen desc', limit=1)

    @api.model
    def _default_printer(self, node, role):
        p = node.printer_ids.filtered(
            lambda x: x.active and x.role == role and x.is_default)[:1]
        if not p:
            p = node.printer_ids.filtered(lambda x: x.active and x.role == role)[:1]
        return p

    @api.model
    def _check_company_allowed(self, company):
        """True when the caller may print for ``company``.

        Defence in depth beside the record rules: these enqueue methods are
        api.model entry points callable over RPC by any POS user, and a later
        sudo() inside them would silently bypass the rules.
        """
        return bool(company) and company.id in self.env.companies.ids

    @api.model
    def enqueue_pos(self, config_id, ref, which='both', printer_id=None):
        """Enqueue label/receipt jobs to the store node (queue path).

        Returns {ok, node, online, job_ids} or {ok:False, error}.
        """
        config = self.env['pos.config'].browse(config_id)
        if not self._check_company_allowed(config.company_id):
            return {'ok': False, 'error': 'company_not_allowed'}
        node = self._node_for_company(config.company_id.id)
        if not node:
            return {'ok': False, 'error': 'no_node_for_store'}
        order = self.env['pos.order']._mint_find_order(ref)
        if not order:
            return {'ok': False, 'error': 'order_not_found'}
        zpl = order._mint_zebra_zpl(which)
        pieces = []
        if which in ('both', 'label') and zpl.get('label'):
            pieces.append(('label', zpl['label']))
        if which in ('both', 'receipt') and zpl.get('receipt'):
            pieces.append(('receipt', zpl['receipt']))
        job_ids = []
        for role, content in pieces:
            printer = (self.env['print.printer'].browse(printer_id)
                       if printer_id else self._default_printer(node, role))
            job = self.create({
                'node_id': node.id,
                'printer_id': printer.id if printer else False,
                'role': role,
                'zpl': content,
                'source': order.pos_reference or order.name,
            })
            job_ids.append(job.id)
        return {'ok': True, 'node': node.name, 'online': node.online,
                'job_ids': job_ids}

    @api.model
    def enqueue_pdf(self, printer_id, pdf_b64, title=None):
        """Queue a PDF to a node printer — rendered by the OS driver (any printer)."""
        printer = self.env['print.printer'].browse(printer_id)
        if not printer.exists():
            return {'ok': False, 'error': 'printer_not_found'}
        if not self._check_company_allowed(printer.node_id.company_id):
            return {'ok': False, 'error': 'company_not_allowed'}
        job = self.create({
            'node_id': printer.node_id.id,
            'printer_id': printer.id,
            'role': printer.role,
            'doc_type': 'pdf',
            'pdf_data': pdf_b64,
            'name': title or 'PDF Document',
            'source': 'pdf:%s' % (title or 'document'),
        })
        return {'ok': True, 'job_id': job.id, 'node': printer.node_id.name,
                'online': printer.node_id.online}
