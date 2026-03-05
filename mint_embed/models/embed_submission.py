# -*- coding: utf-8 -*-
"""
Stores form submissions received via the embed widget
(contact messages and newsletter signups).
"""
from odoo import fields, models


class MintEmbedSubmission(models.Model):
    _name = 'mint.embed.submission'
    _description = 'Embed Form Submission'
    _order = 'create_date desc'

    form_type = fields.Selection([
        ('contact', 'Contact'),
        ('newsletter', 'Newsletter'),
    ], string='Type', required=True, index=True)

    name = fields.Char(string='Name')
    email = fields.Char(string='Email', required=True, index=True)
    phone = fields.Char(string='Phone')
    message = fields.Text(string='Message')
    source_url = fields.Char(string='Source URL',
                             help='Page where the form was submitted')
    embed_key = fields.Char(string='Embed Key',
                            help='Which embed config generated this submission')
    ip_address = fields.Char(string='IP Address')

    state = fields.Selection([
        ('new', 'New'),
        ('read', 'Read'),
        ('replied', 'Replied'),
        ('archived', 'Archived'),
    ], string='Status', default='new', index=True)

    company_id = fields.Many2one('res.company', string='Store',
                                 default=lambda self: self.env.company)

    def action_mark_read(self):
        self.write({'state': 'read'})

    def action_mark_replied(self):
        self.write({'state': 'replied'})

    def action_mark_archived(self):
        self.write({'state': 'archived'})
