# -*- coding: utf-8 -*-
"""Wizard: upload a PDF and print it to any node printer via the OS driver."""
from odoo import api, fields, models, _
from odoo.exceptions import UserError


class PrintDocumentWizard(models.TransientModel):
    _name = 'print.document.wizard'
    _description = 'Print a Document to a Node Printer'

    node_id = fields.Many2one('print.node', string='Print Node', required=True)
    printer_id = fields.Many2one(
        'print.printer', string='Printer', required=True,
        domain="[('node_id', '=', node_id), ('active', '=', True)]",
        help='Any printer on the node — Zebra or not (PDF is rendered by the '
             'OS driver).')
    pdf_file = fields.Binary(string='PDF File', required=True)
    filename = fields.Char(string='File Name')
    copies = fields.Integer(default=1)

    @api.onchange('node_id')
    def _onchange_node(self):
        printers = self.node_id.printer_ids.filtered('active')
        self.printer_id = printers[:1].id if printers else False

    def action_print(self):
        self.ensure_one()
        if not self.pdf_file:
            raise UserError(_('Upload a PDF first.'))
        Job = self.env['print.job']
        ids = []
        for _i in range(max(1, self.copies or 1)):
            res = Job.enqueue_pdf(self.printer_id.id, self.pdf_file,
                                  self.filename or 'Document')
            if not res.get('ok'):
                raise UserError(_('Could not queue: %s') % res.get('error'))
            ids.append(res['job_id'])
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {
                'type': 'success', 'sticky': False,
                'message': _('Queued %s job(s) to %s on node "%s"%s.') % (
                    len(ids), self.printer_id.name, self.printer_id.node_id.name,
                    '' if self.printer_id.node_id.online else ' (node OFFLINE — will print when it reconnects)'),
            },
        }
