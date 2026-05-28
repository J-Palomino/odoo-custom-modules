from odoo import api, fields, models


class DealSubmissionOption(models.Model):
    _name = 'mint.deal.submission.option'
    _description = 'Deal Submission Option — one discount spec on a multi-option submission'
    _order = 'submission_id, sequence, id'

    submission_id = fields.Many2one(
        'mint.deal.submission',
        string='Submission',
        required=True,
        ondelete='cascade',
        index=True,
    )
    sequence = fields.Integer(string='Sequence', default=10)

    discount_type = fields.Selection(
        selection=[
            ('percent', 'Percentage Off'),
            ('fixed', 'Fixed Amount Off'),
            ('bogo', 'BOGO'),
            ('bundle', 'Bundle Deal'),
            ('price', 'Set Price'),
            ('points_multiplier', 'Loyalty Points Multiplier'),
            ('clearance', 'Clearance (Near Expiry)'),
        ],
        string='Discount Type',
        required=True,
    )
    discount_value = fields.Float(
        string='Discount Value',
        help='For points_multiplier, this is the points multiplier (e.g. 2.0 = 2x points).',
    )
    sales_details = fields.Text(
        string='Sales Details',
        help='Optional per-option override for the customer-facing pricing text. '
             'Falls back to the parent submission sales_details when blank.',
    )

    display_text = fields.Char(
        string='Display Text',
        compute='_compute_display_text',
        store=True,
        help='Auto-formatted pricing display for this option.',
    )

    @api.depends('discount_type', 'discount_value', 'sales_details',
                 'submission_id.original_price')
    def _compute_display_text(self):
        # Mirrors the formatter on mint.ptl.deal.option so the vendor-facing
        # submission and the internal ptl.deal show the same string for the
        # same inputs.
        for rec in self:
            if rec.sales_details:
                rec.display_text = rec.sales_details
                continue

            msrp = rec.submission_id.original_price
            val = rec.discount_value
            dtype = rec.discount_type

            if dtype == 'percent' and val:
                pct = val if val > 1 else val * 100
                if msrp:
                    sale = msrp * (1 - pct / 100)
                    rec.display_text = f"~~${msrp:.0f}~~ ${sale:.2f} | {pct:.0f}% Off"
                else:
                    rec.display_text = f"{pct:.0f}% Off"
            elif dtype == 'fixed' and val:
                if msrp:
                    sale = msrp - val
                    rec.display_text = f"~~${msrp:.0f}~~ ${sale:.2f} | ${val:.0f} Off"
                else:
                    rec.display_text = f"${val:.0f} Off"
            elif dtype == 'price' and val:
                if msrp:
                    rec.display_text = f"~~${msrp:.0f}~~ ${val:.2f}"
                else:
                    rec.display_text = f"${val:.2f}"
            elif dtype == 'bogo':
                rec.display_text = f"Starting @ ${msrp:.0f} | BOGO" if msrp else "Buy One Get One"
            elif dtype == 'bundle' and val:
                rec.display_text = f"${msrp:.0f} Value! Only ${val:.2f}" if msrp else f"${val:.2f} Bundle"
            elif dtype == 'points_multiplier' and val:
                mult = val if val >= 1 else (1 / val if val else 0)
                rec.display_text = (
                    f"{int(mult)}x Points" if mult == int(mult) else f"{mult:.1f}x Points"
                )
            elif dtype == 'clearance':
                pct = (val if val > 1 else val * 100) if val else 0
                if msrp and pct:
                    sale = msrp * (1 - pct / 100)
                    rec.display_text = f"Clearance: ~~${msrp:.0f}~~ ${sale:.2f} | {pct:.0f}% Off"
                elif pct:
                    rec.display_text = f"Clearance: {pct:.0f}% Off"
                else:
                    rec.display_text = "Clearance"
            else:
                rec.display_text = ''
