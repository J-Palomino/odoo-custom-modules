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
import base64
import secrets

from odoo import api, fields, models

from . import escpos_receipt


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
        help='Secret the local agent uses to register/poll. Keep private.')
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
    printer_lang = fields.Selection(
        [('zpl', 'ZPL / Zebra'), ('escpos', 'ESC/POS')],
        default='zpl', required=True,
        help='Command language this printer understands. Zebra label printers '
             'speak ZPL; Star/Epson/Citizen receipt printers speak ESC/POS. '
             'Receipt jobs are built in the matching language for the target '
             'printer (see print.job.enqueue_pos).')
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
        [('zpl', 'ZPL / raw'), ('pdf', 'PDF (OS driver)'), ('escpos', 'ESC/POS')],
        default='zpl', required=True,
        help='ZPL/raw text bytes go straight to the printer; PDF is rendered by '
             'the OS print driver (works on non-Zebra printers); ESC/POS is raw '
             'binary carried base64 in pdf_data and printed verbatim (Star/Epson '
             'receipt printers, whose commands include NUL bytes a text column '
             'cannot hold).')
    zpl = fields.Text(help='Raw ZPL / printer commands (for doc_type=zpl).')
    pdf_data = fields.Binary(string='PDF / raw', attachment=True,
                             help='Base64 payload: a PDF for doc_type=pdf, or raw '
                                  'ESC/POS bytes for doc_type=escpos.')
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
    def enqueue_pos(self, config_id, ref, which='both', printer_id=None):
        """Enqueue label/receipt jobs to the store node (queue path).

        Returns {ok, node, online, job_ids} or {ok:False, error}.
        """
        config = self.env['pos.config'].browse(config_id)
        node = self._node_for_company(config.company_id.id)
        if not node:
            return {'ok': False, 'error': 'no_node_for_store'}
        order = self.env['pos.order']._mint_find_order(ref)
        if not order:
            return {'ok': False, 'error': 'order_not_found'}

        # Resolve the target printer per role first: the receipt language depends
        # on it. A ZPL (Zebra) receipt printer gets the ZPL receipt as before; an
        # ESC/POS (Star/Epson) receipt printer gets native ESC/POS bytes instead,
        # since sending it ZPL would print garbage and never fire the drawer.
        zpl = order._mint_zebra_zpl(which)
        data = None  # order dict, built lazily only if an ESC/POS receipt is needed

        plan = []
        if which in ('both', 'label'):
            plan.append(('label', self.env['print.printer'].browse(printer_id)
                         if printer_id else self._default_printer(node, 'label')))
        if which in ('both', 'receipt'):
            plan.append(('receipt', self.env['print.printer'].browse(printer_id)
                         if printer_id else self._default_printer(node, 'receipt')))

        job_ids = []
        for role, printer in plan:
            vals = {
                'node_id': node.id,
                'printer_id': printer.id if printer else False,
                'role': role,
                'source': order.pos_reference or order.name,
            }
            if role == 'receipt' and printer and printer.printer_lang == 'escpos':
                if data is None:
                    data = order._mint_zebra_data()
                payload = escpos_receipt.build_receipt_escpos(
                    data, open_drawer=bool(config.mint_escpos_open_drawer))
                if not payload:
                    continue
                vals['doc_type'] = 'escpos'
                vals['pdf_data'] = base64.b64encode(payload)
            else:
                content = zpl.get(role)
                if not content:
                    continue
                vals['doc_type'] = 'zpl'
                vals['zpl'] = content
            job = self.create(vals)
            job_ids.append(job.id)
        return {'ok': True, 'node': node.name, 'online': node.online,
                'job_ids': job_ids}

    @api.model
    def enqueue_pdf(self, printer_id, pdf_b64, title=None):
        """Queue a PDF to a node printer — rendered by the OS driver (any printer)."""
        printer = self.env['print.printer'].browse(printer_id)
        if not printer.exists():
            return {'ok': False, 'error': 'printer_not_found'}
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
