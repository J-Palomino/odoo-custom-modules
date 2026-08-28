"""Push a validated purchase receipt to Dutchie POS.

## Why validation, not PO confirmation

Confirming a purchase order means the goods are ORDERED. Pushing then would
create inventory in Dutchie for product that is not on the shelf, and the
register would happily sell it. The receipt being validated is the moment the
goods physically exist, so that is the hook. mintinvsvc's endpoint agrees — it
is called odoo-purchase-receive and takes a picking, not a purchase order.

## Why this never raises

A warehouse must be able to receive goods when Dutchie is unreachable, when the
LSP mapping is missing, or when a SKU does not match. Blocking button_validate
on an outbound HTTP call would mean a Dutchie outage stops physical receiving,
which is a far worse failure than a receipt that has to be pushed again. Every
failure is recorded on the picking and in the chatter instead, and
`action_retry_dutchie_push` re-runs it once the cause is fixed.

## Why it is off by default

The endpoint this calls creates a COMPLETED Dutchie receive — it moves real
stock. `mint.dutchie_receive.mode` gates it exactly as it gates the manual
receive screen, and defaults to dry-run, so installing this changes nothing
until someone turns it on deliberately. mintinvsvc additionally refuses any
LocId outside its own allowlist.
"""
import json
import logging
import urllib.error
import urllib.request

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

MODE_PARAM = 'mint.dutchie_receive.mode'
URL_PARAM = 'mint.dutchie_receive.url'
KEY_PARAM = 'mint.dutchie_receive.api_key'
KEY_PARAM_FALLBACK = 'dutchie.publish.api_key'
DEFAULT_URL = 'https://mintinvsvc-production-6aa5.up.railway.app'


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    dutchie_receive_id = fields.Char(
        string='Dutchie Receive ID', readonly=True, copy=False,
        help='ReceiveInventoryHistoryId returned by Dutchie. Its presence is '
             'what proves the receipt reached POS, and is what stops a second '
             'validation pushing the same goods twice.',
    )
    dutchie_push_state = fields.Selection(
        [('pending', 'Not pushed'),
         ('skipped', 'Skipped'),
         ('pushed', 'Pushed'),
         ('failed', 'Failed')],
        string='Dutchie Push', readonly=True, copy=False, default='pending',
    )
    dutchie_push_note = fields.Text(
        string='Dutchie Push Note', readonly=True, copy=False,
        help='Why the push was skipped or how it failed.',
    )

    # ------------------------------------------------------------------

    def button_validate(self):
        res = super().button_validate()

        # button_validate returns an action when it needs a wizard first
        # (backorder, immediate transfer). Nothing is received yet in that
        # case — this method runs again once the wizard confirms.
        if isinstance(res, dict) and res.get('type') == 'ir.actions.act_window':
            return res

        for picking in self:
            picking._push_receipt_to_dutchie()
        return res

    def action_retry_dutchie_push(self):
        """Re-run the push after fixing whatever made it skip or fail."""
        for picking in self:
            picking._push_receipt_to_dutchie(manual=True)
        return True

    # ------------------------------------------------------------------

    def _push_receipt_to_dutchie(self, manual=False):
        """Send this receipt to Dutchie. Swallows every error by design."""
        self.ensure_one()
        try:
            skip = self._dutchie_push_skip_reason()
            if skip:
                # Only record a skip when someone asked explicitly. Otherwise
                # every outgoing delivery and internal transfer in the database
                # would carry a note explaining it is not a purchase receipt.
                if manual:
                    self.write({'dutchie_push_state': 'skipped',
                                'dutchie_push_note': skip})
                return False

            payload = self._dutchie_receive_payload()
            if not payload['lines']:
                self.write({'dutchie_push_state': 'skipped',
                            'dutchie_push_note': _('No received quantities to push.')})
                return False

            result = self._dutchie_post('webhook/odoo-purchase-receive', payload)
            return self._record_dutchie_result(result)

        except Exception as e:  # noqa: BLE001 - receiving must never be blocked
            _logger.exception("Dutchie push failed for picking %s", self.name)
            self.write({'dutchie_push_state': 'failed',
                        'dutchie_push_note': str(e)[:4000]})
            self.message_post(body=_(
                "Could not push this receipt to Dutchie: %s\n\nThe goods are "
                "received in Odoo. Fix the cause and use Retry Dutchie Push — "
                "nothing was sent to POS."
            ) % e)
            return False

    def _dutchie_push_skip_reason(self):
        """Return a human reason to skip, or False to proceed."""
        self.ensure_one()
        if self.picking_type_code != 'incoming':
            return _('Not an incoming transfer.')
        if self.state != 'done':
            return _('Transfer is not validated yet.')
        if self.dutchie_receive_id:
            return _('Already pushed as Dutchie receive %s.') % self.dutchie_receive_id
        if not self.purchase_id and not self.origin:
            return _('Not linked to a purchase order.')
        if not self.company_id.dutchie_pos_location_id:
            return _(
                '%s has no Dutchie POS LocId, so there is nowhere to receive into.'
            ) % self.company_id.name

        mode = (self.env['ir.config_parameter'].sudo().get_param(MODE_PARAM)
                or 'dry-run').strip().lower()
        if mode != 'live':
            return _(
                "Dutchie receive is in '%(mode)s' mode. Set the system parameter "
                "'%(param)s' to 'live' to push receipts to POS."
            ) % {'mode': mode, 'param': MODE_PARAM}
        return False

    def _dutchie_receive_payload(self):
        self.ensure_one()
        lines = []
        for move in self.move_ids:
            # `quantity` is what was actually picked; product_uom_qty is only
            # what was ordered. Pushing demand instead of receipt would put
            # stock in Dutchie that never arrived.
            qty = move.quantity
            if qty <= 0:
                continue
            product = move.product_id
            cost = 0.0
            if move.purchase_line_id:
                cost = move.purchase_line_id.price_unit or 0.0
            if not cost:
                cost = product.standard_price or 0.0
            lines.append({
                'sku': product.default_code or '',
                'product_name': product.name or '',
                'quantity': qty,
                'unit_cost': cost,
                'category': product.x_category or '',
                'net_weight': product.x_weight_grams or product.weight or 0.0,
            })

        partner = self.partner_id or self.purchase_id.partner_id
        return {
            'picking_id': self.id,
            'purchase_order': (self.purchase_id.name if self.purchase_id
                               else (self.origin or self.name)),
            'company_id': self.company_id.id,
            'company_name': self.company_id.name,
            'vendor_id': partner.id if partner else 0,
            'vendor_name': partner.name if partner else '',
            'lines': lines,
        }

    def _record_dutchie_result(self, result):
        """Fold the webhook response onto the picking and the chatter."""
        self.ensure_one()
        receive_id = result.get('dutchie_receive_id')
        skipped = result.get('skipped') or []

        if not result.get('ok') or not receive_id:
            note = result.get('message') or result.get('error') or json.dumps(result)[:1000]
            self.write({'dutchie_push_state': 'failed',
                        'dutchie_push_note': note[:4000]})
            self.message_post(body=_(
                "Dutchie did not accept this receipt: %s"
            ) % note)
            return False

        self.write({
            'dutchie_receive_id': str(receive_id),
            'dutchie_push_state': 'pushed',
            'dutchie_push_note': (
                _('%s line(s) not matched in Dutchie.') % len(skipped) if skipped else False
            ),
        })
        body = _(
            "Pushed to Dutchie — receive #%(rid)s, %(n)s line(s) into LocId %(loc)s."
        ) % {'rid': receive_id, 'n': result.get('received') or 0,
             'loc': self.company_id.dutchie_pos_location_id}
        if skipped:
            # Named individually: Dutchie accepts a partial receive silently, so
            # an unlisted skip becomes stock that is short with no record of why.
            detail = ', '.join(
                f"{s.get('sku') or '(no SKU)'} — {s.get('reason') or 'unmatched'}"
                for s in skipped[:20]
            )
            body += _("\n\n%(n)s line(s) were NOT received: %(detail)s") % {
                'n': len(skipped), 'detail': detail,
            }
        self.message_post(body=body)
        _logger.info("picking %s: %s", self.name, body)
        return True

    def _dutchie_post(self, path, payload, timeout=120):
        get_param = self.env['ir.config_parameter'].sudo().get_param
        base = (get_param(URL_PARAM) or DEFAULT_URL).rstrip('/')
        api_key = get_param(KEY_PARAM) or get_param(KEY_PARAM_FALLBACK)
        if not api_key:
            raise ValueError(
                "No mintinvsvc API key configured (%s)." % KEY_PARAM
            )
        req = urllib.request.Request(
            f"{base}/api/{path}",
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
            # Parsed rather than re-raised: the webhook puts the actionable
            # reason (unknown vendor, no LocId, LocId not allowed) in the body,
            # and it should reach the chatter instead of a bare HTTP 422.
            try:
                return json.loads(body or '{}')
            except ValueError:
                raise ValueError(f"HTTP {e.code}: {body or e.reason}") from e
