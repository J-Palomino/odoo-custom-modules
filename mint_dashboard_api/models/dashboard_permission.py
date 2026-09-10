# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.exceptions import ValidationError


class MintDashboardPermission(models.Model):
    """Per-user permission set for the marketing dashboard.

    Mirrors the dashboard's client-side shape (zzz src/lib/types.ts:
    ModulePermissions = per-module {view, edit, delete} flags plus
    market_access slugs) so the same matrix the sidebar renders is what the
    API enforces server-side. One record per internal user.
    """
    _name = 'mint.dashboard.permission'
    _description = 'Marketing Dashboard Permission Set'
    _rec_name = 'user_id'

    user_id = fields.Many2one(
        'res.users', required=True, ondelete='cascade', index=True,
        domain=[('share', '=', False)],
        help='Internal (non-portal) user this permission set applies to.')
    role_template = fields.Char(default='Custom')
    market_ids = fields.Many2many(
        'mint.region', string='Market Access',
        help='Markets whose data this user may see. Empty = no market access.')
    module_permissions = fields.Json(
        default=dict,
        help='Per-module {"view": bool, "edit": bool, "delete": bool} flags, '
             'keyed by dashboard module name (dashboard, events, vendors, '
             'contacts, tasks, users, requests, ptl, hotbox, ...).')
    notification_prefs = fields.Json(default=dict)
    active = fields.Boolean(default=True)

    @api.constrains('user_id')
    def _check_one_per_user(self):
        for rec in self:
            if self.search_count([('user_id', '=', rec.user_id.id),
                                  ('id', '!=', rec.id)]):
                raise ValidationError(
                    'User %s already has a dashboard permission set.'
                    % rec.user_id.login)

    def has_module_permission(self, module, action='view'):
        self.ensure_one()
        mod = (self.module_permissions or {}).get(module) or {}
        return bool(mod.get(action))

    def market_slugs(self):
        self.ensure_one()
        return self.market_ids.mapped('slug')

    def as_payload(self):
        """JSON shape the dashboard frontend hydrates from (UserPermissions)."""
        self.ensure_one()
        return {
            'user_id': self.user_id.id,
            'role_template': self.role_template or 'Custom',
            'market_access': self.market_slugs(),
            'module_permissions': self.module_permissions or {},
            'notification_prefs': self.notification_prefs or {},
        }
