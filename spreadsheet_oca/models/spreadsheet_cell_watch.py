# Copyright 2026 Mint Cannabis
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import api, fields, models


class SpreadsheetCellWatch(models.Model):
    _name = "spreadsheet.cell.watch"
    _description = "Spreadsheet Cell Watch"
    _order = "spreadsheet_id, name"

    name = fields.Char(required=True)
    spreadsheet_id = fields.Many2one(
        "spreadsheet.spreadsheet", required=True, ondelete="cascade", index=True
    )
    cell_ref = fields.Char(
        required=True,
        help="A1-style reference including sheet, e.g. Sheet1!B5",
    )
    value_type = fields.Selection(
        [("number", "Number"), ("text", "Text"), ("bool", "Boolean")],
        default="number",
        required=True,
    )
    value_number = fields.Float()
    value_text = fields.Char()
    value_bool = fields.Boolean()
    last_updated = fields.Datetime(readonly=True)
    last_pushed_by = fields.Many2one("res.users", readonly=True)
    active = fields.Boolean(default=True)

    _sql_constraints = [
        (
            "uniq_watch_per_cell",
            "unique(spreadsheet_id, cell_ref)",
            "Only one watch per (spreadsheet, cell).",
        ),
    ]

    @api.model
    def apply_sync_payload(self, payload):
        for entry in payload:
            watch = self.browse(entry["watch_id"]).exists()
            if not watch or not watch.active:
                continue
            vals = {"last_updated": fields.Datetime.now(), "last_pushed_by": self.env.uid}
            value = entry.get("value")
            if watch.value_type == "number":
                vals["value_number"] = float(value) if value is not None else 0.0
            elif watch.value_type == "bool":
                vals["value_bool"] = bool(value)
            else:
                vals["value_text"] = "" if value is None else str(value)
            watch.write(vals)
        return True
