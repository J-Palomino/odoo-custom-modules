# -*- coding: utf-8 -*-
"""
Zebra label designer + previewer.

A backend tool to CONFIGURE a Zebra (ZD410, ZPL) label, PREVIEW it as an
image before printing, and optionally TEST PRINT it via PrintNode.

Preview rendering uses Labelary (http://labelary.com), a ZPL->PNG service.
Only label template/sample data is sent — no order or customer PII — but if
that is a concern, Labelary is self-hostable (swap the base URL in the
System Parameter ``mint_pos_dutchie.labelary_url``).
"""
import base64
import json
import logging
import urllib.request
import urllib.error

from odoo import api, fields, models, _
from odoo.exceptions import UserError

from . import zebra_zpl

_logger = logging.getLogger(__name__)

DEFAULT_LABELARY = 'http://api.labelary.com/v1/printers'

# Sample data shown in a fresh designer record / used to preview built-ins.
PRODUCT_SAMPLE = {
    "store_line": "Mint IL Craft, LLC | 540 N Dearborn St. PO Box 10080 Chicago IL 60610-10080",
    "product_name": "MC - Flower - Garlic OG (I) (3.5g)",
    "lot": "1A4150300000DB2000025546",
    "pkg_date": "03/18/2026", "use_by": "12/05/2026",
    "thc": "23.80%", "cbd": "0.00%", "thca": "26.41%", "cbda": "0.00%",
    "lab": "LK Pure Labs",
    "test_date": "12/10/2025", "harvest_date": "12/05/2025",
    "ingredients": "Cured Cannabis Flower",
    "usable_weight": "3.500(g)",
    "microbials": "Pass", "mycotoxins": "Pass",
    "solvents": "Pass", "pesticides": "Pass",
    "url": "wtfextracts.com/espanol",
    "barcode": "1A41503000003265000000077",
}

RECEIPT_SAMPLE = {
    "store": "Mint Tempe", "address": "660 W Warner Rd, Tempe, AZ",
    "order_ref": "Order 00012-001-0007", "date": "2026-06-02 16:45",
    "cashier": "Jane D.", "customer": "John Doe", "item_count": 3,
    "items": [{"name": "Blue Dream 3.5g", "qty": 1, "price": 35.0, "total": 38.15},
              {"name": "Wyld Gummies 100mg", "qty": 2, "price": 18.0, "total": 39.24}],
    "subtotal": 71.0, "tax": 6.39, "total": 77.39, "currency": "$",
    "footer": "Keep out of reach of children.", "barcode": "120010007",
}


class ZebraLabel(models.Model):
    _name = 'mint.zebra.label'
    _description = 'Zebra Label Designer'
    _order = 'name'

    name = fields.Char(required=True, default='New Label')
    kind = fields.Selection(
        [
            ('product', 'Product / Compliance Label'),
            ('exit', 'Order Exit Label'),
            ('receipt', 'Receipt'),
            ('custom', 'Custom ZPL'),
        ],
        string='Label Type', default='product', required=True,
        help='Built-in layouts render from the generator; "Custom ZPL" uses '
             'your own template with {placeholder} tokens.',
    )
    width_in = fields.Float(string='Width (in)', default=2.0, required=True)
    length_in = fields.Float(string='Length (in)', default=4.0, required=True)
    dpi = fields.Selection(
        [('203', '203 dpi'), ('300', '300 dpi')], default='203', required=True)
    darkness = fields.Integer(string='Darkness (0-30)', default=30)

    sample_json = fields.Text(
        string='Sample Data (JSON)',
        default=lambda self: json.dumps(PRODUCT_SAMPLE, indent=2),
        help='Field values used to render the preview and built-in layouts.',
    )
    zpl_template = fields.Text(
        string='Custom ZPL',
        help='Raw ZPL with {token} placeholders filled from Sample Data. '
             'Used only when Label Type = Custom ZPL.',
    )

    preview_image = fields.Binary(string='Preview', readonly=True, attachment=True)
    preview_zpl = fields.Text(string='Rendered ZPL', readonly=True)
    preview_error = fields.Char(string='Preview Error', readonly=True)

    printnode_printer_id = fields.Integer(
        string='PrintNode Printer ID',
        help='Used by Test Print. Falls back to a POS config value if 0. The '
             'PrintNode API key lives in System Parameter '
             'mint_pos_dutchie.printnode_api_key.',
    )
    node_id = fields.Many2one(
        'mint.print.node', string='Print Node',
        help='Print this label to a store print node (queued; the node agent '
             'prints it). Lets you configure + print remotely from this GUI.')
    node_printer_id = fields.Many2one(
        'mint.print.printer', string='Node Printer',
        domain="[('node_id', '=', node_id)]",
        help='Printer on the node to send to. Defaults to the node label '
             'printer if blank.')

    # ── rendering ────────────────────────────────────────────────────
    def _sample(self):
        self.ensure_one()
        try:
            return json.loads(self.sample_json or '{}')
        except (ValueError, TypeError) as exc:
            raise UserError(_('Sample Data is not valid JSON: %s') % exc)

    def _resolve_zpl(self):
        self.ensure_one()
        data = self._sample()
        dpi = int(self.dpi)
        kw = dict(dpi=dpi, width_in=self.width_in or 2.0,
                  length_in=self.length_in or 4.0,
                  darkness=self.darkness or 20)
        if self.kind == 'product':
            return zebra_zpl.build_product_label_zpl(data, **kw)
        if self.kind == 'exit':
            return zebra_zpl.build_label_zpl(data, **kw)
        if self.kind == 'receipt':
            return zebra_zpl.build_receipt_zpl(data, **kw)
        # custom: substitute {token} placeholders from sample data
        template = self.zpl_template or ''
        for key, value in data.items():
            template = template.replace('{%s}' % key, str(value))
        return template

    def _labelary_url(self):
        base = self.env['ir.config_parameter'].sudo().get_param(
            'mint_pos_dutchie.labelary_url', DEFAULT_LABELARY)
        dpmm = 12 if self.dpi == '300' else 8
        return '%s/%ddpmm/labels/%sx%s/0/' % (
            base.rstrip('/'), dpmm,
            ('%g' % (self.width_in or 2.0)), ('%g' % (self.length_in or 4.0)))

    def _labelary_png(self, zpl):
        req = urllib.request.Request(
            self._labelary_url(),
            data=zpl.encode('utf-8'),
            headers={'Accept': 'image/png', 'Content-Type': 'application/x-www-form-urlencoded'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read()

    def _do_preview(self):
        for rec in self:
            try:
                zpl = rec._resolve_zpl()
                png = rec._labelary_png(zpl)
                rec.with_context(skip_preview=True).write({
                    'preview_image': base64.b64encode(png),
                    'preview_zpl': zpl,
                    'preview_error': False,
                })
            except urllib.error.HTTPError as err:
                detail = err.read().decode('utf-8', 'replace')[:300]
                rec.with_context(skip_preview=True).write(
                    {'preview_error': 'Labelary %s: %s' % (err.code, detail)})
            except Exception as exc:  # noqa: BLE001
                _logger.warning('Zebra label preview failed: %s', exc)
                rec.with_context(skip_preview=True).write(
                    {'preview_error': str(exc)[:300]})

    _PREVIEW_FIELDS = {
        'kind', 'width_in', 'length_in', 'dpi', 'darkness',
        'sample_json', 'zpl_template',
    }

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._do_preview()
        return records

    def write(self, vals):
        res = super().write(vals)
        if not self.env.context.get('skip_preview') and \
                self._PREVIEW_FIELDS & set(vals):
            self._do_preview()
        return res

    # ── actions ──────────────────────────────────────────────────────
    def action_refresh_preview(self):
        self._do_preview()
        return True

    def action_load_builtin_into_custom(self):
        """Copy the generated ZPL into the Custom ZPL editor for tweaking."""
        self.ensure_one()
        if self.kind == 'custom':
            raise UserError(_('Pick a built-in Label Type first, then load it.'))
        self.with_context(skip_preview=True).write({
            'zpl_template': self._resolve_zpl(),
            'kind': 'custom',
        })
        self._do_preview()
        return True

    def action_test_print(self):
        """Send the rendered ZPL to PrintNode (server-side)."""
        self.ensure_one()
        zpl = self._resolve_zpl()
        printer_id = self.printnode_printer_id
        if not printer_id:
            cfg = self.env['pos.config'].search(
                [('mint_printnode_printer_id', '!=', 0)], limit=1)
            printer_id = cfg.mint_printnode_printer_id if cfg else 0
        if not printer_id:
            raise UserError(_(
                'Set a PrintNode Printer ID on this label or a POS config.'))
        api_key = self.env['ir.config_parameter'].sudo().get_param(
            'mint_pos_dutchie.printnode_api_key', '')
        if not api_key:
            raise UserError(_(
                'Set System Parameter mint_pos_dutchie.printnode_api_key.'))

        auth = base64.b64encode(('%s:' % api_key).encode()).decode()
        payload = {
            'printerId': printer_id,
            'title': 'Label preview: %s' % self.name,
            'contentType': 'raw_base64',
            'content': base64.b64encode(zpl.encode('utf-8')).decode(),
            'source': 'Odoo Zebra Label Designer',
        }
        try:
            req = urllib.request.Request(
                'https://api.printnode.com/printjobs',
                data=json.dumps(payload).encode('utf-8'),
                headers={'Content-Type': 'application/json',
                         'Authorization': 'Basic %s' % auth},
                method='POST')
            with urllib.request.urlopen(req, timeout=15) as resp:
                job = resp.read().decode('utf-8').strip()
        except urllib.error.HTTPError as err:
            detail = err.read().decode('utf-8', 'replace')[:300]
            raise UserError(_('PrintNode error %s: %s') % (err.code, detail))
        except Exception as exc:  # noqa: BLE001
            raise UserError(_('PrintNode request failed: %s') % exc)

        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'type': 'success', 'sticky': False,
                       'message': _('Sent to PrintNode (job %s).') % job},
        }

    def action_print_to_node(self):
        """Queue the rendered ZPL to a store print node (remote printing).

        The node's agent polls the queue and prints — so this works from any
        Odoo GUI session, anywhere.
        """
        self.ensure_one()
        node = self.node_id
        if not node:
            raise UserError(_('Pick a Print Node first.'))
        printer = self.node_printer_id
        if not printer:
            printer = self.env['mint.print.job']._default_printer(node, 'label')
        job = self.env['mint.print.job'].create({
            'node_id': node.id,
            'printer_id': printer.id if printer else False,
            'role': 'label',
            'zpl': self._resolve_zpl(),
            'source': 'designer:%s' % self.name,
        })
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {
                'type': 'success', 'sticky': False,
                'message': _('Queued to node "%s"%s. Job #%s — the node agent '
                             'will print it.') % (
                    node.name,
                    (' (printer %s)' % printer.name) if printer else '',
                    job.id),
            },
        }

    @api.onchange('kind')
    def _onchange_kind_sample(self):
        """Swap in matching sample data when switching built-in type."""
        if self.kind == 'receipt' and 'product_name' in (self.sample_json or ''):
            self.sample_json = json.dumps(RECEIPT_SAMPLE, indent=2)
        elif self.kind in ('product', 'exit') and 'order_ref' in (self.sample_json or ''):
            self.sample_json = json.dumps(PRODUCT_SAMPLE, indent=2)
