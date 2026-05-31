# -*- coding: utf-8 -*-
import base64
import logging
from urllib.parse import quote

from odoo import models, fields, api

_logger = logging.getLogger(__name__)

# Preview rendered in the form; the download button serves a higher-res copy.
_PREVIEW_PX = 256
_DOWNLOAD_PX = 512


class LinkTracker(models.Model):
    _inherit = 'link.tracker'

    qr_code = fields.Binary(
        string='QR Code',
        compute='_compute_qr_code',
        help="Scannable QR code that points at this tracked short URL.",
    )
    qr_code_filename = fields.Char(
        string='QR Code Filename',
        compute='_compute_qr_code',
    )

    @api.depends('short_url', 'code')
    def _compute_qr_code(self):
        report = self.env['ir.actions.report']
        for link in self:
            target = link.short_url
            if not target:
                link.qr_code = False
                link.qr_code_filename = False
                continue
            try:
                png = report.barcode(
                    'QR', target, width=_PREVIEW_PX, height=_PREVIEW_PX,
                )
                link.qr_code = base64.b64encode(png)
                link.qr_code_filename = 'qr-%s.png' % (link.code or link.id)
            except Exception:  # pragma: no cover - defensive: never break the form
                _logger.warning(
                    "Failed to render QR for link.tracker %s (%s)",
                    link.id, target, exc_info=True,
                )
                link.qr_code = False
                link.qr_code_filename = False

    def action_download_qr(self):
        """Open Odoo's built-in barcode route to download a high-res PNG."""
        self.ensure_one()
        if not self.short_url:
            return False
        url = (
            '/report/barcode/?barcode_type=QR&value=%s&width=%d&height=%d'
            % (quote(self.short_url, safe=''), _DOWNLOAD_PX, _DOWNLOAD_PX)
        )
        return {
            'type': 'ir.actions.act_url',
            'url': url,
            'target': 'new',
        }
