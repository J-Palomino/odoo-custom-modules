# Copyright 2024 Tecnativa - Carlos Roca
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import json

from odoo.exceptions import AccessError
from odoo.http import Controller, content_disposition, request, route


class SpreadsheetCellWatchSync(Controller):
    @route(
        "/spreadsheet_oca/watch/sync",
        type="json",
        auth="user",
        methods=["POST"],
    )
    def sync(self, spreadsheet_id, values):
        sheet = request.env["spreadsheet.spreadsheet"].browse(int(spreadsheet_id))
        try:
            sheet.check_access_rights("read")
            sheet.check_access_rule("read")
        except AccessError:
            return {"ok": False, "error": "access_denied"}
        watches = request.env["spreadsheet.cell.watch"].search(
            [("spreadsheet_id", "=", sheet.id), ("active", "=", True)]
        )
        allowed_ids = set(watches.ids)
        payload = [v for v in values if int(v.get("watch_id", 0)) in allowed_ids]
        request.env["spreadsheet.cell.watch"].apply_sync_payload(payload)
        return {"ok": True, "applied": len(payload)}


class SpreadsheetDownloadXLSX(Controller):
    @route("/spreadsheet/xlsx", type="http", auth="user", methods=["POST"])
    def download_spreadsheet_xlsx(self, zip_name, files, **kw):
        if hasattr(files, "read"):
            files = files.read().decode("utf-8")
        files = json.loads(files)
        file_content = request.env["spreadsheet.mixin"]._zip_xslx_files(files)
        return request.make_response(
            file_content,
            [
                ("Content-Length", len(file_content)),
                ("Content-Type", "application/vnd.ms-excel"),
                ("X-Content-Type-Options", "nosniff"),
                ("Content-Disposition", content_disposition(zip_name)),
            ],
        )
