"""Odoo-side intake for a Dutchie inventory receive.

The GUI a store uses to take a delivery in and push it to Dutchie POS.

## Why two Dutchie calls and not one

Dutchie exposes two relevant paths and they do different things:

  * `v2/inventory/preview-receive-url` — takes a URL, fetches it from Dutchie's
    own infrastructure, parses it as a WCIA Transfer Data Schema v2.1.0
    manifest, and returns the lines with each SKU resolved to a real Dutchie
    ProductId. It is a **preview**: it VALIDATES WITHOUT PERSISTING. No
    commit-by-URL endpoint has been found (see the probe log in
    dutchie-csv-import-schema-and-url-receive).
  * `v2/inventory/receive` — the direct API. This is the one that actually
    creates PackageInventory rows. Verified persisting against AZ LocId 1989
    on 2026-04-17.

So `action_validate` uses the link (cheap, safe, resolves SKUs and catches a
bad delivery before anyone signs off) and `action_push` uses the direct API
(the thing that actually moves inventory). Each path is used for what it is
proven to do. If Dutchie ever ships a commit-by-URL endpoint, only
`_invsvc('receive/commit', ...)` on the invsvc side has to change.

## Why the work happens in mintinvsvc and not here

The Dutchie Backoffice session/LSP handling, the SKU resolver, the room and
vendor lookups and the LSP-per-LocId map all already live in
`packages/inventory-service/api/dutchieBackoffice.js`, behind guards that
refuse any write without an explicit LocId + LspId (a session-LSP fallback is
how a June-1 discount batch leaked onto the wrong stores). Re-implementing any
of that in Odoo would fork those guards. Odoo sends lines; invsvc talks to
Dutchie.
"""
import json
import logging
import urllib.error
import urllib.request

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# System-parameter knobs, mirroring mint.dutchie_discount_push.*
MODE_PARAM = 'mint.dutchie_receive.mode'          # off | dry-run | live
URL_PARAM = 'mint.dutchie_receive.url'            # mintinvsvc base URL
KEY_PARAM = 'mint.dutchie_receive.api_key'
# One API key gates every /api route on invsvc, so the discount-push key is a
# legitimate fallback and saves ops configuring the same secret twice.
KEY_PARAM_FALLBACK = 'dutchie.publish.api_key'
DEFAULT_URL = 'https://mintinvsvc-production-6aa5.up.railway.app'

RECEIVE_TYPES = [
    ('Purchase', 'Purchase'),
    ('Transfer', 'Transfer'),
    ('Return', 'Return'),
]

STATES = [
    ('draft', 'Draft'),
    ('validated', 'Validated'),
    ('approved', 'Approved'),
    ('pushed', 'Pushed to Dutchie'),
    ('rejected', 'Rejected'),
]


class DutchieReceive(models.Model):
    _name = 'mint.dutchie.receive'
    _description = 'Dutchie Inventory Receive'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'create_date desc'

    name = fields.Char(string='Reference', readonly=True, copy=False, default='New')
    state = fields.Selection(
        STATES, string='Status', default='draft',
        required=True, tracking=True, copy=False,
    )

    # ------------------------------------------------------------------
    # Destination
    # ------------------------------------------------------------------
    company_id = fields.Many2one(
        'res.company', string='Store', required=True,
        default=lambda self: self.env.company, tracking=True,
        # The requirement is "has a Dutchie POS location", not "is a storefront".
        # Filtering on is_dispensary would exclude the Dutchie sandboxes, which
        # are not Mint storefronts and are exactly where this gets tested.
        domain="[('dutchie_pos_location_id', '!=', 0)]",
        help='Destination store. Its Dutchie POS LocId is what the receive '
             'targets, so only companies that have one can be selected.',
    )
    pos_location_id = fields.Integer(
        string='Dutchie LocId', related='company_id.dutchie_pos_location_id',
        readonly=True, store=True,
        help='Integer POS LocId Dutchie uses for this store. Set on the company '
             'record — a store with 0 here cannot receive.',
    )
    # Not stored on res.company on purpose: invsvc derives it from the LocId via
    # lspForLoc() so there is exactly one source of truth for the mapping.
    lsp_id = fields.Integer(
        string='Dutchie LspId', readonly=True, copy=False, tracking=True,
        help='Resolved by mintinvsvc from the LocId during Validate.',
    )

    # ------------------------------------------------------------------
    # Delivery header
    # ------------------------------------------------------------------
    receive_type = fields.Selection(
        RECEIVE_TYPES, string='Receive Type', default='Purchase', required=True,
    )
    partner_id = fields.Many2one(
        'res.partner', string='Vendor',
        domain="[('supplier_rank', '>', 0)]",
        help='Odoo-side vendor. Used to suggest the Dutchie vendor by name.',
    )
    dutchie_vendor_id = fields.Integer(
        string='Dutchie VendorId', tracking=True, copy=False,
        help='Required by v2/inventory/receive. Auto-suggested on Validate by '
             'name-matching against the vendors Dutchie has for this location; '
             'override here if the match is wrong.',
    )
    dutchie_vendor_name = fields.Char(string='Dutchie Vendor', copy=False)
    vendor_license = fields.Char(string='Vendor License')
    invoice_number = fields.Char(
        string='Invoice / PO Number',
        help='Free-text reference shown on the Dutchie receipt.',
    )
    delivered_by = fields.Char(string='Delivered By')
    delivered_on = fields.Datetime(string='Delivered On')
    received_by = fields.Char(string='Received By')
    note = fields.Text(string='Note')

    dutchie_room_id = fields.Integer(
        string='Destination RoomId', tracking=True, copy=False,
        help='Dutchie room the packages land in. A receive with no room orphans '
             'in Dutchie. Auto-suggested on Validate (prefers a room with '
             'InventoryRoom = yes).',
    )
    dutchie_room_name = fields.Char(string='Destination Room', copy=False)

    line_ids = fields.One2many(
        'mint.dutchie.receive.line', 'receive_id', string='Lines', copy=True,
    )

    commit_mode = fields.Selection(
        [('saved', 'Draft in Dutchie (no stock movement)'),
         ('complete', 'Complete receive (moves stock)')],
        string='Push As', default='saved', required=True, tracking=True,
        help="Draft calls v2/inventory/save-receive — the same endpoint Dutchie's "
             "own import wizard uses. It lands in the location's saved orders "
             "with no packages and no stock movement, for someone to complete "
             "in Backoffice against the physical delivery. Complete calls "
             "v2/inventory/receive, which creates packages and moves stock now.",
    )
    preview_items_json = fields.Text(
        string='Preview Items (raw)', readonly=True, copy=False,
        help='Dutchie preview response, verbatim. A draft push reshapes THIS '
             'rather than rebuilding from the lines — the preview already '
             'resolved SKU to ProductId and matched the vendor, and rebuilding '
             'would discard the lab data, item totals and transaction id.',
    )

    # ------------------------------------------------------------------
    # Validation artifacts
    # ------------------------------------------------------------------
    manifest_url = fields.Char(
        string='Manifest URL', readonly=True, copy=False,
        help='Capability URL hosting the WCIA manifest that Dutchie fetched. '
             'Short-TTL and fetch-capped — expect it to be dead shortly after '
             'validation. Kept for the audit trail.',
    )
    manifest_id = fields.Integer(string='Manifest ID', readonly=True, copy=False)
    manifest_expires_at = fields.Datetime(
        string='Manifest Expires', readonly=True, copy=False,
    )
    validated_at = fields.Datetime(string='Validated At', readonly=True, copy=False)
    preview_response = fields.Text(
        string='Dutchie Preview Response', readonly=True, copy=False,
        help='Raw response from preview-receive-url, kept verbatim for debugging.',
    )

    # ------------------------------------------------------------------
    # Approval + push
    # ------------------------------------------------------------------
    approved_by = fields.Many2one('res.users', string='Approved By', readonly=True, copy=False)
    approved_at = fields.Datetime(string='Approved At', readonly=True, copy=False)
    rejected_by = fields.Many2one('res.users', string='Rejected By', readonly=True, copy=False)
    rejected_reason = fields.Text(string='Rejection Reason', readonly=True, copy=False)

    dutchie_receive_id = fields.Char(
        string='Dutchie Receive ID', readonly=True, copy=False, tracking=True,
        help='ReceiveInventoryHistoryId returned by v2/inventory/receive. Its '
             'presence is what proves the inventory actually landed.',
    )
    pushed_by = fields.Many2one('res.users', string='Pushed By', readonly=True, copy=False)
    pushed_at = fields.Datetime(string='Pushed At', readonly=True, copy=False)
    error_message = fields.Text(string='Last Error', readonly=True, copy=False)

    # ------------------------------------------------------------------
    # Computed
    # ------------------------------------------------------------------
    line_count = fields.Integer(compute='_compute_totals')
    resolved_count = fields.Integer(compute='_compute_totals')
    unresolved_count = fields.Integer(compute='_compute_totals')
    total_quantity = fields.Float(compute='_compute_totals')
    total_cost = fields.Float(compute='_compute_totals')

    @api.depends('line_ids', 'line_ids.quantity', 'line_ids.unit_cost',
                 'line_ids.dutchie_product_id')
    def _compute_totals(self):
        for rec in self:
            lines = rec.line_ids
            rec.line_count = len(lines)
            rec.resolved_count = len(lines.filtered(lambda line: line.dutchie_product_id))
            rec.unresolved_count = len(lines) - rec.resolved_count
            rec.total_quantity = sum(lines.mapped('quantity'))
            rec.total_cost = sum(line.quantity * line.unit_cost for line in lines)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'mint.dutchie.receive'
                ) or 'New'
        return super().create(vals_list)

    # ------------------------------------------------------------------
    # invsvc transport
    # ------------------------------------------------------------------

    def _invsvc(self, path, payload, timeout=120):
        """POST to mintinvsvc and return the parsed body.

        Raises UserError (not a bare exception) on every failure so the message
        surfaces in the Odoo dialog instead of a 500 page. An HTTPError body is
        included verbatim — invsvc puts the Dutchie-side reason there and it is
        the only diagnostic a store manager will ever get.
        """
        get_param = self.env['ir.config_parameter'].sudo().get_param
        base = (get_param(URL_PARAM) or DEFAULT_URL).rstrip('/')
        api_key = get_param(KEY_PARAM) or get_param(KEY_PARAM_FALLBACK)
        if not api_key:
            raise UserError(_(
                "No mintinvsvc API key configured. Set the system parameter "
                "'%s' before receiving inventory."
            ) % KEY_PARAM)

        req = urllib.request.Request(
            f"{base}/api/dutchie/{path}",
            data=json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json', 'x-api-key': api_key},
            method='POST',
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode(errors='replace') or '{}')
        except urllib.error.HTTPError as e:
            body = ''
            try:
                body = e.read().decode(errors='replace')[:2000]
            except Exception:
                pass
            _logger.warning("dutchie receive %s: HTTP %s %s", path, e.code, body)
            raise UserError(_(
                "mintinvsvc returned HTTP %(code)s for %(path)s.\n\n%(body)s"
            ) % {'code': e.code, 'path': path, 'body': body or e.reason}) from e
        except Exception as e:
            _logger.warning("dutchie receive %s failed: %s", path, e)
            raise UserError(_(
                "Could not reach mintinvsvc (%(path)s): %(err)s"
            ) % {'path': path, 'err': e}) from e

    def _mode(self):
        return (self.env['ir.config_parameter'].sudo().get_param(MODE_PARAM)
                or 'dry-run').strip().lower()

    def _check_ready(self):
        """Guards shared by Validate and Push."""
        self.ensure_one()
        if not self.line_ids:
            raise UserError(_("Add at least one line before sending anything to Dutchie."))
        if not self.pos_location_id:
            raise UserError(_(
                "%s has no Dutchie POS LocId. Set dutchie_pos_location_id on the "
                "company record before receiving inventory into it."
            ) % self.company_id.name)
        missing_sku = self.line_ids.filtered(lambda line: not (line.sku or '').strip())
        if missing_sku:
            raise UserError(_(
                "Every line needs a SKU — it is the only key Dutchie matches on. "
                "Missing on %s line(s)."
            ) % len(missing_sku))
        bad_qty = self.line_ids.filtered(lambda line: line.quantity <= 0)
        if bad_qty:
            raise UserError(_(
                "Quantity must be greater than zero. %s line(s) are at or below zero."
            ) % len(bad_qty))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_validate(self):
        """Host a WCIA manifest and have Dutchie parse it. Persists nothing."""
        self.ensure_one()
        if self.state not in ('draft', 'validated', 'rejected'):
            raise UserError(_("Only a draft or validated receive can be validated."))
        self._check_ready()
        if self._mode() == 'off':
            raise UserError(_(
                "Dutchie receive is switched off. Set the system parameter "
                "'%s' to 'dry-run' or 'live' to enable it."
            ) % MODE_PARAM)

        payload = {
            'locId': self.pos_location_id,
            'receiveType': self.receive_type,
            'invoiceNumber': self.invoice_number or '',
            'vendorName': self.dutchie_vendor_name or (self.partner_id.name or ''),
            'vendorLicense': self.vendor_license or '',
            'reference': self.name,
            'lines': [line._to_manifest_dict() for line in self.line_ids],
        }
        result = self._invsvc('receive/validate', payload)

        if not result.get('ok'):
            self.write({'error_message': json.dumps(result, indent=2)[:8000]})
            raise UserError(_(
                "Dutchie rejected the manifest:\n\n%s"
            ) % (result.get('error') or json.dumps(result)[:1500]))

        self._apply_validation(result)

        via = result.get('resolvedVia') or 'preview-url'
        summary = _(
            "Validated against Dutchie. %(resolved)s of %(total)s SKUs matched "
            "(via %(via)s)."
        ) % {'resolved': self.resolved_count, 'total': self.line_count, 'via': via}
        if via != 'preview-url':
            # The manifest link is the mechanism under test; when it does not
            # answer, say so rather than letting a silent fallback read as a
            # healthy round-trip.
            summary += _(
                "\nThe manifest link did not answer, so SKUs were matched by "
                "direct lookup instead. Reason: %s"
            ) % (result.get('previewError') or _('no lines returned'))
        self.message_post(body=summary)
        _logger.info("%s: %s (locId=%s lspId=%s)",
                     self.name, summary, self.pos_location_id, self.lsp_id)
        return True

    def _apply_validation(self, result):
        """Fold a validate response back onto the record and its lines."""
        self.ensure_one()
        by_sku = {}
        for entry in result.get('lines') or []:
            key = (entry.get('sku') or '').strip().lower()
            if key:
                by_sku[key] = entry

        for line in self.line_ids:
            entry = by_sku.get((line.sku or '').strip().lower())
            if not entry:
                line.write({
                    'dutchie_product_id': 0,
                    'resolve_note': _('Not returned by Dutchie'),
                })
                continue
            line.write({
                'dutchie_product_id': entry.get('dutchieProductId') or 0,
                'dutchie_product_name': entry.get('productName') or '',
                'unit_id': entry.get('unitId') or line.unit_id or 1,
                'resolve_note': '' if entry.get('dutchieProductId')
                                else (entry.get('reason') or _('SKU not found in Dutchie')),
            })

        vals = {
            'state': 'validated',
            'lsp_id': result.get('lspId') or self.lsp_id,
            'manifest_url': result.get('manifestUrl') or '',
            'manifest_id': result.get('manifestId') or 0,
            'validated_at': fields.Datetime.now(),
            'preview_response': json.dumps(result.get('preview') or {}, indent=2)[:16000],
            'preview_items_json': json.dumps(result.get('previewItems') or []),
            'error_message': False,
        }
        expires = result.get('expiresAt')
        if expires:
            # Dutchie/invsvc hand back ISO 8601 with a Z; Odoo wants naive UTC.
            vals['manifest_expires_at'] = expires.replace('T', ' ').replace('Z', '')[:19]
        # Only fill suggestions the user has not already overridden.
        if not self.dutchie_room_id and result.get('suggestedRoomId'):
            vals['dutchie_room_id'] = result['suggestedRoomId']
            vals['dutchie_room_name'] = result.get('suggestedRoomName') or ''
        if not self.dutchie_vendor_id and result.get('suggestedVendorId'):
            vals['dutchie_vendor_id'] = result['suggestedVendorId']
            vals['dutchie_vendor_name'] = result.get('suggestedVendorName') or ''
        # Dutchie's own VendorCode is what belongs on the licence field — it is
        # what from_license_number carries. Never type a guessed state licence
        # here: receiving against an unknown code creates a junk vendor record.
        if not self.vendor_license and result.get('suggestedVendorLicense'):
            vals['vendor_license'] = result['suggestedVendorLicense']
        self.write(vals)

    def action_approve(self):
        self.ensure_one()
        if self.state != 'validated':
            raise UserError(_("Validate the receive against Dutchie before approving it."))
        if self.unresolved_count:
            raise UserError(_(
                "%(n)s line(s) did not match a Dutchie SKU. Fix or remove them and "
                "re-validate — pushing would silently drop them.\n\nUnmatched: %(skus)s"
            ) % {
                'n': self.unresolved_count,
                'skus': ', '.join(
                    self.line_ids.filtered(lambda line: not line.dutchie_product_id)
                    .mapped('sku')[:20]
                ),
            })
        self.write({
            'state': 'approved',
            'approved_by': self.env.uid,
            'approved_at': fields.Datetime.now(),
        })

    def action_reject(self):
        self.ensure_one()
        if self.state not in ('validated', 'approved'):
            raise UserError(_("Only a validated or approved receive can be rejected."))
        self.write({
            'state': 'rejected',
            'rejected_by': self.env.uid,
        })

    def action_reset_draft(self):
        self.ensure_one()
        if self.state == 'pushed':
            raise UserError(_(
                "This receive is already in Dutchie (%s). Reverse it in Dutchie "
                "rather than re-opening it here."
            ) % self.dutchie_receive_id)
        self.write({'state': 'draft'})

    def action_push(self):
        """Commit to Dutchie via v2/inventory/receive. This one persists."""
        self.ensure_one()
        if self.state != 'approved':
            raise UserError(_("Only an approved receive can be pushed to Dutchie."))
        self._check_ready()

        mode = self._mode()
        if mode != 'live':
            raise UserError(_(
                "Dutchie receive is in '%(mode)s' mode, so nothing was pushed. Set "
                "the system parameter '%(param)s' to 'live' to commit receives."
            ) % {'mode': mode, 'param': MODE_PARAM})

        if not self.lsp_id:
            raise UserError(_("LspId is unknown — re-validate to resolve it."))
        if not self.dutchie_room_id:
            raise UserError(_(
                "A destination RoomId is required. The manifest carries no room "
                "concept, so Dutchie's preview returns RoomId null — without one "
                "here a completed receive orphans with no PackageInventory rows."
            ))

        if self.commit_mode == 'saved':
            return self._push_draft()

        if not self.dutchie_vendor_id:
            raise UserError(_(
                "A Dutchie VendorId is required — v2/inventory/receive rejects the "
                "call without one. Re-validate to get a suggestion, or set it by hand."
            ))

        products = [line._to_receive_product() for line in self.line_ids]
        payload = {
            'commitMode': 'complete',
            'locId': self.pos_location_id,
            'lspId': self.lsp_id,
            'vendorId': self.dutchie_vendor_id,
            'vendorName': self.dutchie_vendor_name or (self.partner_id.name or ''),
            'vendorLicense': self.vendor_license or '',
            'invoiceNumber': self.invoice_number or self.name,
            'orderTitle': f"{self.name} — {self.dutchie_vendor_name or ''}".strip(' —'),
            'receiveType': self.receive_type,
            'defaultRoomId': self.dutchie_room_id,
            'receivedBy': self.received_by or self.env.user.name,
            'deliveredBy': self.delivered_by or '',
            'deliveredOn': self.delivered_on and self.delivered_on.isoformat() or '',
            'note': self.note or '',
            'products': products,
        }

        result = self._invsvc('receive/commit', payload)
        receive_id = result.get('dutchieReceiveId')
        if not result.get('ok') or not receive_id:
            self.write({'error_message': json.dumps(result, indent=2)[:8000]})
            raise UserError(_(
                "Dutchie did not confirm the receive:\n\n%s"
            ) % (result.get('error') or json.dumps(result)[:1500]))

        return self._land_push(receive_id, len(products), moved_stock=True)

    def _push_draft(self):
        """Create a DRAFT receive in Dutchie's saved orders. Moves no stock.

        Sends the preview response back verbatim: save-receive reshapes it
        rather than rebuilding, so the lab data, item totals and transaction id
        Dutchie resolved during validation survive into the draft.
        """
        self.ensure_one()
        try:
            items = json.loads(self.preview_items_json or '[]')
        except (ValueError, TypeError):
            items = []
        if not items:
            raise UserError(_(
                "No stored Dutchie preview to push. A draft is built from the "
                "preview response, not from these lines — re-validate first. "
                "(If validation fell back to a direct SKU lookup, the manifest "
                "link did not answer and there is no preview to send.)"
            ))

        result = self._invsvc('receive/commit', {
            'commitMode': 'saved',
            'locId': self.pos_location_id,
            'lspId': self.lsp_id,
            'defaultRoomId': self.dutchie_room_id,
            'orderTitle': f"{self.name} — {self.dutchie_vendor_name or ''}".strip(' \u2014'),
            'previewItems': items,
        })
        receive_id = result.get('dutchieReceiveId')
        if not result.get('ok') or not receive_id:
            self.write({'error_message': json.dumps(result, indent=2)[:8000]})
            raise UserError(_(
                "Dutchie did not confirm the draft:\n\n%s"
            ) % (result.get('error') or json.dumps(result)[:1500]))
        return self._land_push(receive_id, len(items), moved_stock=False)

    def _land_push(self, receive_id, count, moved_stock):
        self.ensure_one()
        self.write({
            'state': 'pushed',
            'dutchie_receive_id': str(receive_id),
            'pushed_by': self.env.uid,
            'pushed_at': fields.Datetime.now(),
            'error_message': False,
        })
        if moved_stock:
            body = _(
                "Completed in Dutchie — receive #%(rid)s, %(n)s product(s) into "
                "room %(room)s at LocId %(loc)s. Stock has moved."
            ) % {
                'rid': receive_id, 'n': count,
                'room': self.dutchie_room_name or self.dutchie_room_id,
                'loc': self.pos_location_id,
            }
        else:
            body = _(
                "Draft created in Dutchie — receive #%(rid)s, %(n)s item(s) at "
                "LocId %(loc)s. No packages were created and no stock moved: "
                "complete it in Backoffice against the physical delivery."
            ) % {'rid': receive_id, 'n': count, 'loc': self.pos_location_id}
        self.message_post(body=body)
        _logger.info("%s: %s", self.name, body)
        return True
