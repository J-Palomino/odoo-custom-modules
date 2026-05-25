import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class ResCompany(models.Model):
    _inherit = "res.company"

    intercompany_sale_enabled = fields.Boolean(
        string="Auto-generate Intercompany SO/PO",
        default=False,
        help="When a Purchase Order is confirmed for a vendor that is "
             "another company in this system, automatically create a "
             "matching Sale Order in the vendor's company.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        companies = super().create(vals_list)
        # Auto-add the creating user to new companies so they aren't
        # locked out by global multi-company record rules.
        user = self.env.user
        if user and not user._is_superuser():
            missing = companies - user.company_ids
            if missing:
                user.sudo().write({
                    "company_ids": [(4, c.id) for c in missing],
                })
                _logger.info(
                    "Auto-added user %s (uid=%s) to new companies: %s",
                    user.name, user.id,
                    ", ".join(missing.mapped("name")),
                )
        return companies


class SaleOrder(models.Model):
    _inherit = "sale.order"

    intercompany_po_id = fields.Many2one(
        "purchase.order",
        string="Source Intercompany PO",
        readonly=True,
        copy=False,
        help="The Purchase Order in the other company that generated this SO.",
    )


class PurchaseOrder(models.Model):
    _inherit = "purchase.order"

    intercompany_so_id = fields.Many2one(
        "sale.order",
        string="Generated Intercompany SO",
        readonly=True,
        copy=False,
        help="The Sale Order auto-created in the vendor's company.",
    )

    def button_confirm(self):
        res = super().button_confirm()
        for order in self:
            order._create_intercompany_sale_order()
        return res

    def _create_intercompany_sale_order(self):
        """If the vendor is another company in this system, auto-create a SO."""
        self.ensure_one()

        if self.intercompany_so_id:
            return  # already created

        vendor = self.partner_id
        # Find a company whose partner matches this vendor
        target_company = self.env["res.company"].sudo().search([
            ("partner_id", "=", vendor.id),
            ("intercompany_sale_enabled", "=", True),
        ], limit=1)

        if not target_company or target_company == self.company_id:
            return

        _logger.info(
            "Creating intercompany SO in %s for PO %s",
            target_company.name, self.name,
        )

        # Map PO lines to SO lines
        so_lines = []
        for line in self.order_line:
            so_lines.append((0, 0, {
                "product_id": line.product_id.id,
                "name": line.name,
                "product_uom_qty": line.product_qty,
                "product_uom_id": line.product_uom_id.id,
                "price_unit": line.price_unit,
            }))

        # Create SO as sudo in the target company
        so = self.env["sale.order"].sudo().with_company(target_company).create({
            "partner_id": self.company_id.partner_id.id,
            "company_id": target_company.id,
            "order_line": so_lines,
            "intercompany_po_id": self.id,
        })

        self.sudo().write({"intercompany_so_id": so.id})

        _logger.info(
            "Intercompany SO %s created in %s (from PO %s in %s)",
            so.name, target_company.name, self.name, self.company_id.name,
        )
