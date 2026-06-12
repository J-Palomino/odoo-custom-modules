# Copyright 2022 CreuBlanca
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import base64
import copy
import json
import re
from typing import Any

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

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
                dashboard.spreadsheet_raw = json.loads(
                    base64.decodebytes(dashboard.spreadsheet_binary_data).decode(
                        "UTF-8"
                    )
                )
            else:
                dashboard.spreadsheet_raw = {}

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

    # ------------------------------------------------------------------
    # Cell-level helpers for headless / MCP edits (Smartsheets MCP, phase 1)
    #
    # The workbook lives in ``spreadsheet_raw`` (o-spreadsheet JSON). In the
    # current format a cell is keyed by its A1 address and its value is the
    # plain content string (a literal, or a formula starting with ``=``):
    #     spreadsheet_raw["sheets"][i]["cells"]["A1"] == "=SUM(B1:B5)"
    # These helpers let a caller read/patch individual cells without shipping
    # the whole workbook, while still routing every WRITE through
    # ``mcp_safe_write`` so the concurrency guards are preserved.
    # ------------------------------------------------------------------

    _CELL_RE = re.compile(r"^([A-Z]+)([0-9]+)$")

    @api.model
    def _mcp_find_sheet(self, raw, sheet=None):
        """Resolve a sheet dict in ``raw`` by id or name (default: first sheet)."""
        sheets = (raw or {}).get("sheets") or []
        if not sheets:
            raise UserError(_("SMARTSHEET_ERROR: workbook has no sheets."))
        if sheet in (None, "", False):
            return sheets[0]
        for sh in sheets:
            if sheet in (sh.get("id"), sh.get("name")):
                return sh
        raise UserError(
            _("SMARTSHEET_ERROR: sheet %s not found (have: %s).")
            % (sheet, ", ".join(s.get("name") or s.get("id") for s in sheets))
        )

    @api.model
    def _mcp_cell_content(self, value):
        """Normalize a stored cell value to its content string (or None)."""
        if value is None:
            return None
        if isinstance(value, str):
            return value
        if isinstance(value, dict):  # tolerate the {"content": ...} variant
            return value.get("content")
        return str(value)

    @api.model
    def _mcp_col_to_num(self, col):
        num = 0
        for ch in col:
            num = num * 26 + (ord(ch) - 64)
        return num

    @api.model
    def _mcp_num_to_col(self, num):
        out = ""
        while num > 0:
            num, rem = divmod(num - 1, 26)
            out = chr(65 + rem) + out
        return out

    @api.model
    def _mcp_expand_range(self, ref):
        """Expand ``A1`` -> ['A1'] and ``A1:B3`` -> all enclosed A1 addresses."""
        ref = (ref or "").strip().upper()
        if ":" not in ref:
            if not self._CELL_RE.match(ref):
                raise UserError(_("SMARTSHEET_ERROR: bad cell ref %s.") % ref)
            return [ref]
        a, b = ref.split(":", 1)
        ma, mb = self._CELL_RE.match(a.strip()), self._CELL_RE.match(b.strip())
        if not ma or not mb:
            raise UserError(_("SMARTSHEET_ERROR: bad range %s.") % ref)
        c1, r1 = self._mcp_col_to_num(ma.group(1)), int(ma.group(2))
        c2, r2 = self._mcp_col_to_num(mb.group(1)), int(mb.group(2))
        if (abs(c2 - c1) + 1) * (abs(r2 - r1) + 1) > 10000:
            raise UserError(
                _("SMARTSHEET_ERROR: range %s is too large (max 10000 cells).") % ref
            )
        return [
            "%s%d" % (self._mcp_num_to_col(c), r)
            for c in range(min(c1, c2), max(c1, c2) + 1)
            for r in range(min(r1, r2), max(r1, r2) + 1)
        ]

    def mcp_read_cells(self, cells=None, sheet=None):
        """Read cell contents from a Smartsheet (headless / MCP).

        :param cells: an A1 ref, an ``'A1:B3'`` range, or a list of those.
            ``None`` returns every non-empty cell on the sheet.
        :param sheet: sheet id or name; defaults to the first sheet.
        :return: dict with ``cells`` ({A1: content}) plus the concurrency
            tokens (``write_date``, ``revision_head``) to feed a later write.
        """
        self.ensure_one()
        self.check_access("read")
        raw = self.spreadsheet_raw or {}
        sh = self._mcp_find_sheet(raw, sheet)
        stored = sh.get("cells") or {}
        if cells in (None, "", False):
            out = {a: self._mcp_cell_content(v) for a, v in stored.items()}
        else:
            refs = cells if isinstance(cells, (list, tuple)) else [cells]
            addrs = [a for ref in refs for a in self._mcp_expand_range(ref)]
            out = {a: self._mcp_cell_content(stored.get(a)) for a in addrs}
        return {
            "id": self.id,
            "sheet": sh.get("name"),
            "sheet_id": sh.get("id"),
            "cells": out,
            "write_date": fields.Datetime.to_string(self.write_date) or False,
            "revision_head": self._spreadsheet_revision_head(),
        }

    def mcp_set_cells(
        self, edits, sheet=None, expected_write_date=None, expected_revision_id=None
    ):
        """Set cell contents / formulas on a Smartsheet, guarded.

        :param edits: list of ``{'cell': 'A1', 'content': '=SUM(B1:B5)'}``. A
            formula is just a ``content`` string starting with ``=``; an empty
            or null ``content`` clears the cell. ``'formula'`` is accepted as an
            alias for ``'content'``.
        :param sheet: sheet id or name; defaults to the first sheet.
        :param expected_write_date / expected_revision_id: optimistic-concurrency
            guards from a prior ``mcp_read_cells`` — the write is refused with a
            ``SMARTSHEET_CONFLICT:`` error if either no longer matches.
        :return: fresh ``{id, write_date, revision_head, cells_written}`` tokens.
        """
        self.ensure_one()
        self.check_access("write")
        if not edits:
            raise UserError(_("SMARTSHEET_ERROR: no edits provided."))
        raw = copy.deepcopy(self.spreadsheet_raw or {})
        sh = self._mcp_find_sheet(raw, sheet)
        sh.setdefault("cells", {})
        written = []
        for edit in edits:
            if not isinstance(edit, dict) or not edit.get("cell"):
                raise UserError(_("SMARTSHEET_ERROR: each edit needs a 'cell'."))
            addr = str(edit["cell"]).strip().upper()
            if not self._CELL_RE.match(addr):
                raise UserError(
                    _("SMARTSHEET_ERROR: %s is not a single cell ref.") % addr
                )
            content = edit.get("content", edit.get("formula"))
            # XML-RPC has no null: a missing value arrives as False -> treat as clear.
            if content in (None, "", False):
                sh["cells"].pop(addr, None)
            else:
                sh["cells"][addr] = str(content)
            written.append(addr)
        result = self.mcp_safe_write(
            {"spreadsheet_raw": raw},
            expected_write_date=expected_write_date,
            expected_revision_id=expected_revision_id,
        )
        result["cells_written"] = written
        return result

    def write(self, vals):
        if "spreadsheet_raw" in vals:
            self.spreadsheet_revision_ids.unlink()
        return super().write(vals)
