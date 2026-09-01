# Copyright 2022 CreuBlanca
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import base64
import json
import logging
from typing import Any

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

_logger = logging.getLogger(__name__)

CollaborationMessage = dict[str, Any]


class SpreadsheetAbstract(models.AbstractModel):
    _name = "spreadsheet.abstract"
    _description = "Spreadsheet abstract for inheritance"
    _inherit = ["bus.listener.mixin"]

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    spreadsheet_binary_data = fields.Binary(
        string="Spreadsheet file",
        default=lambda self: self._empty_spreadsheet_data_base64(),
    )
    spreadsheet_raw = fields.Json(
        inverse="_inverse_spreadsheet_raw", compute="_compute_spreadsheet_raw"
    )
    spreadsheet_revision_ids = fields.One2many(
        "spreadsheet.oca.revision",
        inverse_name="res_id",
        domain=lambda r: [("model", "=", r._name)],
    )

    @api.depends("spreadsheet_binary_data")
    def _compute_spreadsheet_raw(self):
        for dashboard in self:
            if dashboard.spreadsheet_binary_data:
                dashboard.spreadsheet_raw = dashboard._decode_spreadsheet_binary_data()
            else:
                dashboard.spreadsheet_raw = dashboard._empty_spreadsheet_data()

    def _spreadsheet_fallback(self, reason):
        """Log why a stored payload was unusable and return an empty workbook.

        The fallback must be a real workbook, not {}: o-spreadsheet imports
        whatever this returns, and an object with no "sheets" crashes it in
        CellPlugin.createCell just as hard as a malformed one would.
        """
        _logger.warning(
            "%s id=%s: spreadsheet_binary_data %s; rendering it empty.",
            self._name,
            self.id,
            reason,
        )
        return self._empty_spreadsheet_data()

    def _decode_spreadsheet_binary_data(self):
        """Return the stored workbook, or an empty one if it is not usable.

        Never raises, and always returns something o-spreadsheet can import.
        This runs from a compute Odoo evaluates over the whole recordset being
        read, so one unusable record would otherwise break the list, kanban and
        search views for every user able to see it.
        """
        self.ensure_one()
        try:
            data = json.loads(
                base64.decodebytes(self.spreadsheet_binary_data).decode("UTF-8")
            )
        except (UnicodeDecodeError, ValueError):
            # Not the UTF-8 JSON this field expects -- e.g. a real .xlsx
            # uploaded under a .json name, or corrupted base64.
            return self._spreadsheet_fallback("is not UTF-8 JSON")
        if not isinstance(data, dict):
            # Double-encoded: json.dumps() applied twice, so this decodes to a
            # str holding the workbook rather than to the workbook itself.
            return self._spreadsheet_fallback(
                "decoded to %s, not a workbook object" % type(data).__name__
            )
        if not data.get("sheets"):
            # Decodes to a dict, but not an o-spreadsheet workbook -- e.g. an
            # .xlsx dumped as {"[Content_Types].xml": ..., "xl/workbook.xml":
            # ...}. Valid JSON and a dict, so only the missing "sheets" tells
            # it apart, and o-spreadsheet crashes on it in the browser.
            return self._spreadsheet_fallback("has no 'sheets'; not a workbook")
        return data

    def _inverse_spreadsheet_raw(self):
        for record in self:
            record.spreadsheet_binary_data = base64.encodebytes(
                json.dumps(record.spreadsheet_raw).encode("UTF-8")
            )

    def _empty_spreadsheet_data_base64(self):
        """Create an empty spreadsheet workbook.
        Encoded as base64
        """
        data = json.dumps(self._empty_spreadsheet_data())
        return base64.b64encode(data.encode())

    def _empty_spreadsheet_data(self):
        """Create an empty spreadsheet workbook.
        The sheet name should be the same for all users to allow consistent references
        in formulas. It is translated for the user creating the spreadsheet.
        """
        lang = self.env["res.lang"]._lang_get(self.env.user.lang)
        locale = lang._odoo_lang_to_spreadsheet_locale()
        return {
            "version": 1,
            "sheets": [
                {
                    "id": "sheet1",
                    "name": _("Sheet1"),
                }
            ],
            "settings": {
                "locale": locale,
            },
            "revisionId": "START_REVISION",
        }

    def get_spreadsheet_data(self):
        self.ensure_one()
        mode = "normal"
        try:
            self.check_access("write")
        except AccessError:
            mode = "readonly"
        return {
            "name": self.name,
            "spreadsheet_raw": self.spreadsheet_raw,
            "revisions": [
                dict(
                    json.loads(revision.commands),
                    nextRevisionId=revision.next_revision_id,
                    serverRevisionId=revision.server_revision_id,
                )
                for revision in self.spreadsheet_revision_ids
            ],
            "mode": mode,
            "default_currency": self.env[
                "res.currency"
            ].get_company_currency_for_spreadsheet(),
            "user_locale": self.env["res.lang"]._get_user_spreadsheet_locale(),
        }

    def open_spreadsheet(self):
        self.ensure_one()
        return {
            "type": "ir.actions.client",
            "tag": "action_spreadsheet_oca",
            "params": {"spreadsheet_id": self.id, "model": self._name},
        }

    def send_spreadsheet_message(
        self, message: CollaborationMessage, access_token=None
    ):
        self.ensure_one()
        if message["type"] in ["REVISION_UNDONE", "REMOTE_REVISION", "REVISION_REDONE"]:
            self._check_access_spreadsheet("write")
            self.env["spreadsheet.oca.revision"].create(
                {
                    "model": self._name,
                    "res_id": self.id,
                    "type": message["type"],
                    "client_id": message.get("clientId"),
                    "next_revision_id": message["nextRevisionId"],
                    "server_revision_id": message["serverRevisionId"],
                    "commands": json.dumps(
                        self._build_spreadsheet_revision_commands_data(message)
                    ),
                }
            )
            self._bus_send(
                "notification", dict(message, id=self.id), subchannel="spreadsheet_oca"
            )
            return True
        elif message["type"] == "SNAPSHOT":
            self._check_access_spreadsheet("write")
            self.env["spreadsheet.oca.revision"].create(
                {
                    "model": self._name,
                    "res_id": self.id,
                    "type": message["type"],
                    "client_id": message.get("clientId"),
                    "next_revision_id": message["nextRevisionId"],
                    "server_revision_id": message["serverRevisionId"],
                    "commands": json.dumps(
                        self._build_spreadsheet_revision_commands_data(message)
                    ),
                }
            )
            return True
        elif message["type"] in ["CLIENT_JOINED", "CLIENT_LEFT", "CLIENT_MOVED"]:
            self._check_access_spreadsheet("read")
            self._bus_send(
                "notification", dict(message, id=self.id), subchannel="spreadsheet_oca"
            )
            return True
        return False

    def _check_access_spreadsheet(self, operation: str):
        try:
            self.check_access(operation)
        except AccessError as e:
            raise e
        return True

    @api.model
    def _build_spreadsheet_revision_commands_data(self, message):
        """Prepare spreadsheet revision commands data from the message"""
        commands = dict(message)
        commands.pop("serverRevisionId", None)
        commands.pop("nextRevisionId", None)
        commands.pop("clientId", None)
        return commands

    def _spreadsheet_revision_head(self):
        """Return the current head revision id of this spreadsheet.

        This is the optimistic-concurrency token for the live collaborative
        layer: the ``next_revision_id`` of the most recent revision, or the
        workbook's own ``revisionId`` when no revisions exist yet. Returns
        ``False`` when neither is available (XML-RPC safe).
        """
        self.ensure_one()
        last = self.env["spreadsheet.oca.revision"].search(
            [("model", "=", self._name), ("res_id", "=", self.id)],
            order="id desc",
            limit=1,
        )
        if last:
            return last.next_revision_id or False
        return (self.spreadsheet_raw or {}).get("revisionId") or False

    def mcp_safe_write(
        self, vals, expected_write_date=None, expected_revision_id=None
    ):
        """Guarded full-workbook write for headless / MCP edits.

        A plain ``write({'spreadsheet_raw': ...})`` is a blind overwrite that
        also unlinks every revision, so it can silently clobber concurrent
        edits. This method makes the check-and-write atomic by taking a
        ``FOR UPDATE`` row lock, then refusing the write if either guard fails:

        * Guard 1 (write_date): the record's ``write_date`` no longer matches
          ``expected_write_date`` — someone wrote since the caller last read.
        * Guard 2 (revision head): the live revision head no longer matches
          ``expected_revision_id`` — a collaborative edit landed since.

        Both guards are skipped when their expected value is omitted, so the
        caller can opt into one, both, or neither (force-overwrite).

        :param dict vals: field values to write (e.g. ``{'spreadsheet_raw': ...}``)
        :param str expected_write_date: write_date the caller last observed
            (``'YYYY-MM-DD HH:MM:SS'``), as returned by a prior read.
        :param str expected_revision_id: revision head the caller last observed.
        :raises UserError: prefixed ``SMARTSHEET_CONFLICT:`` when a guard fails,
            so the RPC caller can detect a conflict vs. a genuine error.
        :return: ``{'id', 'write_date', 'revision_head'}`` fresh tokens to chain
            the next guarded write.
        """
        self.ensure_one()
        self.check_access("write")
        # Lock the row so the guard check and the write commit as one unit —
        # a concurrent writer blocks here instead of racing between check/write.
        self.env.cr.execute(
            'SELECT write_date FROM "%s" WHERE id = %%s FOR UPDATE' % self._table,
            (self.id,),
        )
        row = self.env.cr.fetchone()
        if not row:
            raise UserError(_("SMARTSHEET_CONFLICT: record no longer exists."))
        current_write_date = fields.Datetime.to_string(row[0]) if row[0] else False
        if expected_write_date and current_write_date != expected_write_date:
            raise UserError(
                _(
                    "SMARTSHEET_CONFLICT: this spreadsheet changed since you "
                    "read it (expected write_date %(exp)s, found %(cur)s). "
                    "Re-read it and retry."
                )
                % {"exp": expected_write_date, "cur": current_write_date}
            )
        if expected_revision_id:
            head = self._spreadsheet_revision_head()
            if head != expected_revision_id:
                raise UserError(
                    _(
                        "SMARTSHEET_CONFLICT: a live edit landed since you read "
                        "this spreadsheet (expected revision %(exp)s, found "
                        "%(cur)s). Re-read it and retry."
                    )
                    % {"exp": expected_revision_id, "cur": head}
                )
        self.write(vals)
        self.invalidate_recordset(["write_date"])
        return {
            "id": self.id,
            "write_date": fields.Datetime.to_string(self.write_date) or False,
            "revision_head": self._spreadsheet_revision_head(),
        }

    def write(self, vals):
        if "spreadsheet_raw" in vals:
            self.spreadsheet_revision_ids.unlink()
        return super().write(vals)
