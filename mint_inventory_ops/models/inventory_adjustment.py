import json
import logging
from datetime import datetime

from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

ADJUSTMENT_TYPES = [
    ('adjust', 'Quantity Adjustment'),
    ('combine', 'Combine Products'),
    ('convert', 'Convert Product'),
    ('move', 'Move / Transfer'),
    ('destroy', 'Destroy'),
    ('discontinue', 'Discontinue'),
    ('restore', 'Restore'),
]

STATES = [
    ('draft', 'Draft'),
    ('submitted', 'Submitted'),
    ('approved', 'Approved'),
    ('done', 'Done'),
    ('rejected', 'Rejected'),
]


class InventoryAdjustment(models.Model):
    _name = 'mint.inventory.adjustment'
    _description = 'Cannabis Inventory Adjustment'
    _inherit = ['mail.thread']
    _order = 'create_date desc'

    name = fields.Char(string='Reference', readonly=True, copy=False, default='New')
    adjustment_type = fields.Selection(
        ADJUSTMENT_TYPES, string='Type', required=True,
        default='adjust', tracking=True,
        states={'done': [('readonly', True)], 'rejected': [('readonly', True)]},
    )
    state = fields.Selection(
        STATES, string='Status', default='draft',
        required=True, tracking=True, copy=False,
    )
    company_id = fields.Many2one(
        'res.company', string='Store', required=True,
        default=lambda self: self.env.company, tracking=True,
    )
    warehouse_id = fields.Many2one(
        'stock.warehouse', string='Warehouse', required=True,
        domain="[('company_id', '=', company_id)]",
    )
    location_id = fields.Many2one(
        'stock.location', string='Stock Location',
        compute='_compute_location_id', store=True,
    )
    dest_warehouse_id = fields.Many2one(
        'stock.warehouse', string='Destination Warehouse',
        domain="[('company_id', '!=', company_id)]",
        help='For inter-store transfers only',
    )
    line_ids = fields.One2many(
        'mint.inventory.adjustment.line', 'adjustment_id',
        string='Lines', copy=True,
    )
    reason = fields.Text(string='Reason / Notes', tracking=True)
    picking_id = fields.Many2one(
        'stock.picking', string='Stock Picking',
        readonly=True, copy=False,
    )

    # Approval
    submitted_by = fields.Many2one('res.users', string='Submitted By', readonly=True, copy=False)
    submitted_at = fields.Datetime(string='Submitted At', readonly=True, copy=False)
    approved_by = fields.Many2one('res.users', string='Approved By', readonly=True, copy=False)
    approved_at = fields.Datetime(string='Approved At', readonly=True, copy=False)
    rejected_by = fields.Many2one('res.users', string='Rejected By', readonly=True, copy=False)
    rejected_reason = fields.Text(string='Rejection Reason', readonly=True, copy=False)

    # Compliance
    metrc_tag = fields.Char(string='Metrc Tag')
    batch_number = fields.Char(string='Batch Number')

    # Computed
    line_count = fields.Integer(compute='_compute_line_count')
    total_quantity = fields.Float(compute='_compute_totals')
    total_value = fields.Float(compute='_compute_totals')

    @api.depends('warehouse_id')
    def _compute_location_id(self):
        for rec in self:
            rec.location_id = rec.warehouse_id.lot_stock_id if rec.warehouse_id else False

    @api.depends('line_ids')
    def _compute_line_count(self):
        for rec in self:
            rec.line_count = len(rec.line_ids)

    @api.depends('line_ids.quantity')
    def _compute_totals(self):
        for rec in self:
            rec.total_quantity = sum(rec.line_ids.mapped('quantity'))
            rec.total_value = sum(
                line.quantity * (line.product_id.standard_price or 0)
                for line in rec.line_ids
            )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'mint.inventory.adjustment'
                ) or 'New'
        return super().create(vals_list)

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def action_submit(self):
        self.ensure_one()
        if not self.line_ids:
            raise UserError(_("Cannot submit an adjustment with no lines."))
        if self.adjustment_type in ('destroy', 'discontinue') and not self.reason:
            raise UserError(_("A reason is required for %s operations.") % self.adjustment_type)
        self.write({
            'state': 'submitted',
            'submitted_by': self.env.uid,
            'submitted_at': fields.Datetime.now(),
        })

    def action_approve(self):
        self.ensure_one()
        if self.state != 'submitted':
            raise UserError(_("Only submitted adjustments can be approved."))
        self.write({
            'state': 'approved',
            'approved_by': self.env.uid,
            'approved_at': fields.Datetime.now(),
        })

    def action_reject(self):
        self.ensure_one()
        if self.state != 'submitted':
            raise UserError(_("Only submitted adjustments can be rejected."))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Reject Adjustment'),
            'res_model': 'mint.inventory.adjustment',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_rejected_reason': '', 'reject_mode': True},
        }

    def action_do_reject(self, reason=''):
        self.ensure_one()
        self.write({
            'state': 'rejected',
            'rejected_by': self.env.uid,
            'rejected_reason': reason or _('No reason given'),
        })

    def action_apply(self):
        self.ensure_one()
        if self.state != 'approved':
            raise UserError(_("Only approved adjustments can be applied."))

        handler = {
            'adjust': self._apply_adjust,
            'combine': self._apply_combine,
            'convert': self._apply_convert,
            'move': self._apply_move,
            'destroy': self._apply_destroy,
            'discontinue': self._apply_discontinue,
            'restore': self._apply_restore,
        }.get(self.adjustment_type)

        if not handler:
            raise UserError(_("Unknown adjustment type: %s") % self.adjustment_type)

        handler()
        self.write({'state': 'done'})
        self.message_post(body=_("Adjustment applied by %s") % self.env.user.name)

        # Auto-sync to external systems
        self._sync_to_avancir()

    def action_reset_draft(self):
        self.ensure_one()
        if self.state not in ('rejected',):
            raise UserError(_("Only rejected adjustments can be reset to draft."))
        self.write({'state': 'draft'})

    # ------------------------------------------------------------------
    # Adjustment operations
    # ------------------------------------------------------------------

    def _apply_adjust(self):
        """Direct quantity adjustment on stock.quant."""
        for line in self.line_ids:
            quant = self.env['stock.quant'].sudo().search([
                ('product_id', '=', line.product_id.id),
                ('location_id', '=', self.location_id.id),
            ], limit=1)
            if quant:
                quant.write({
                    'quantity': line.new_qty,
                    'inventory_quantity': line.new_qty,
                })
            else:
                self.env['stock.quant'].sudo().create({
                    'product_id': line.product_id.id,
                    'location_id': self.location_id.id,
                    'quantity': line.new_qty,
                    'inventory_quantity': line.new_qty,
                })

    def _apply_combine(self):
        """Combine quantities from multiple products into one target."""
        if len(self.line_ids) < 2:
            raise UserError(_("Combine requires at least 2 lines."))
        # First line is the target, rest are sources
        target_line = self.line_ids[0]
        source_lines = self.line_ids[1:]
        total = target_line.current_qty
        for line in source_lines:
            total += line.quantity
            # Zero out source quants
            quant = self.env['stock.quant'].sudo().search([
                ('product_id', '=', line.product_id.id),
                ('location_id', '=', self.location_id.id),
            ], limit=1)
            if quant:
                quant.write({'quantity': 0, 'inventory_quantity': 0})
        # Set target quantity
        target_quant = self.env['stock.quant'].sudo().search([
            ('product_id', '=', target_line.product_id.id),
            ('location_id', '=', self.location_id.id),
        ], limit=1)
        if target_quant:
            target_quant.write({'quantity': total, 'inventory_quantity': total})

    def _apply_convert(self):
        """Convert product from one form to another (e.g. flower → pre-rolls)."""
        # Same as adjust — sets new quantities on each line
        self._apply_adjust()

    def _apply_move(self):
        """Inter-warehouse transfer via stock.picking."""
        if not self.dest_warehouse_id:
            raise UserError(_("Destination warehouse is required for move operations."))

        picking_type = self.env['stock.picking.type'].search([
            ('code', '=', 'internal'),
            ('warehouse_id', '=', self.warehouse_id.id),
        ], limit=1)
        if not picking_type:
            raise UserError(_("No internal transfer type found for warehouse %s.") % self.warehouse_id.name)

        picking = self.env['stock.picking'].create({
            'picking_type_id': picking_type.id,
            'location_id': self.location_id.id,
            'location_dest_id': self.dest_warehouse_id.lot_stock_id.id,
            'origin': self.name,
            'company_id': self.company_id.id,
        })

        for line in self.line_ids:
            self.env['stock.move'].create({
                'name': f'{self.name}: {line.product_id.name}',
                'product_id': line.product_id.id,
                'product_uom_qty': line.quantity,
                'product_uom': line.product_id.uom_id.id,
                'picking_id': picking.id,
                'location_id': self.location_id.id,
                'location_dest_id': self.dest_warehouse_id.lot_stock_id.id,
            })

        picking.action_confirm()
        picking.action_assign()
        self.picking_id = picking

    def _apply_destroy(self):
        """Move products to scrap location."""
        scrap_location = self.env['stock.location'].search([
            ('scrap_location', '=', True),
            ('company_id', 'in', [self.company_id.id, False]),
        ], limit=1)
        if not scrap_location:
            raise UserError(_("No scrap location found. Please configure one in Inventory settings."))

        for line in self.line_ids:
            self.env['stock.scrap'].create({
                'product_id': line.product_id.id,
                'scrap_qty': line.quantity,
                'location_id': self.location_id.id,
                'origin': f'{self.name} - Destroy',
                'company_id': self.company_id.id,
            }).action_validate()

            # Update product status (columns pre-created via SQL, no ORM field)
            self.env.cr.execute("""
                UPDATE product_template
                SET inventory_status = 'destroyed', last_adjustment_id = %s
                WHERE id = %s
            """, (self.id, line.product_id.product_tmpl_id.id))

    def _apply_discontinue(self):
        """Mark products as discontinued and zero out stock."""
        for line in self.line_ids:
            # Zero the quant
            quant = self.env['stock.quant'].sudo().search([
                ('product_id', '=', line.product_id.id),
                ('location_id', '=', self.location_id.id),
            ], limit=1)
            if quant:
                quant.write({'quantity': 0, 'inventory_quantity': 0})

            # Update product status (columns pre-created via SQL, no ORM field)
            self.env.cr.execute("""
                UPDATE product_template
                SET inventory_status = 'discontinued',
                    discontinue_reason = %s,
                    discontinue_date = %s,
                    last_adjustment_id = %s
                WHERE id = %s
            """, (self.reason, fields.Date.today(), self.id, line.product_id.product_tmpl_id.id))

    def _apply_restore(self):
        """Restore discontinued/destroyed products to active status."""
        for line in self.line_ids:
            # Update product status (columns pre-created via SQL, no ORM field)
            self.env.cr.execute("""
                UPDATE product_template
                SET inventory_status = 'active',
                    discontinue_reason = NULL,
                    discontinue_date = NULL,
                    last_adjustment_id = %s
                WHERE id = %s
            """, (self.id, line.product_id.product_tmpl_id.id))
            # Set restored quantity
            if line.new_qty:
                quant = self.env['stock.quant'].sudo().search([
                    ('product_id', '=', line.product_id.id),
                    ('location_id', '=', self.location_id.id),
                ], limit=1)
                if quant:
                    quant.write({'quantity': line.new_qty, 'inventory_quantity': line.new_qty})
                else:
                    self.env['stock.quant'].sudo().create({
                        'product_id': line.product_id.id,
                        'location_id': self.location_id.id,
                        'quantity': line.new_qty,
                        'inventory_quantity': line.new_qty,
                    })

    # ------------------------------------------------------------------
    # External sync
    # ------------------------------------------------------------------

    def _sync_to_avancir(self):
        """Push adjustment results to Avancir RFID system."""
        try:
            AvancirSync = self.env.get('avancir.sync')
            if not AvancirSync:
                return

            for line in self.line_ids:
                avancir_id = line.product_id.product_tmpl_id.avancir_item_id
                if not avancir_id:
                    continue

                # Read inventory_status from DB directly (column exists, no ORM field)
                self.env.cr.execute(
                    "SELECT inventory_status FROM product_template WHERE id = %s",
                    (line.product_id.product_tmpl_id.id,)
                )
                row = self.env.cr.fetchone()
                inv_status = row[0] if row else 'active'

                status_map = {
                    'active': 'Active',
                    'discontinued': 'Inactive',
                    'destroyed': 'Destroyed',
                    'hold': 'On Hold',
                    'quarantine': 'Quarantine',
                }
                new_status = status_map.get(inv_status, 'Active')
                try:
                    sync = AvancirSync.sudo()
                    sync.update_avancir_item_status(avancir_id, new_status)
                    _logger.info(
                        "Avancir sync: item %s → %s (adj %s by %s)",
                        avancir_id, new_status, self.name, self.submitted_by.name,
                    )
                except Exception as e:
                    _logger.warning("Avancir sync failed for item %s: %s", avancir_id, e)

        except Exception as e:
            _logger.warning("Avancir sync skipped: %s", e)

    # ------------------------------------------------------------------
    # API serialization
    # ------------------------------------------------------------------

    def _to_api_dict(self):
        self.ensure_one()
        return {
            'id': self.id,
            'name': self.name,
            'type': self.adjustment_type,
            'state': self.state,
            'company_id': self.company_id.id,
            'company_name': self.company_id.name,
            'warehouse_id': self.warehouse_id.id,
            'warehouse_name': self.warehouse_id.name,
            'dest_warehouse_id': self.dest_warehouse_id.id if self.dest_warehouse_id else None,
            'dest_warehouse_name': self.dest_warehouse_id.name if self.dest_warehouse_id else None,
            'reason': self.reason or '',
            'metrc_tag': self.metrc_tag or '',
            'batch_number': self.batch_number or '',
            'submitted_by': self.submitted_by.name if self.submitted_by else None,
            'submitted_at': self.submitted_at and self.submitted_at.isoformat() or None,
            'approved_by': self.approved_by.name if self.approved_by else None,
            'approved_at': self.approved_at and self.approved_at.isoformat() or None,
            'line_count': self.line_count,
            'total_quantity': self.total_quantity,
            'lines': [line._to_api_dict() for line in self.line_ids],
            'picking_id': self.picking_id.id if self.picking_id else None,
            'create_date': self.create_date and self.create_date.isoformat() or None,
        }
