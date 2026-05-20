from odoo import api, fields, models


class TextDealsWizard(models.TransientModel):
    _name = 'mint.ptl.text.deals.wizard'
    _description = 'Export PTL Day deals as plain text (SMS / POS-paste friendly)'

    day_id = fields.Many2one(
        'mint.ptl.day',
        string='PTL Day',
        required=True,
    )
    output_text = fields.Text(
        string='Text Deals',
        compute='_compute_output_text',
        readonly=True,
    )

    @api.depends('day_id')
    def _compute_output_text(self):
        for wiz in self:
            wiz.output_text = (
                wiz.day_id._compose_text_deals() if wiz.day_id else ''
            )

    def action_download_txt(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_url',
            'url': f'/mint/ptl/{self.day_id.id}/text-deals',
            'target': 'new',
        }
