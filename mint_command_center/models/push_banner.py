from odoo import fields, models

CATEGORY_SELECTION = [
    ('flower', 'Flower'),
    ('pre-rolls', 'Pre-Rolls'),
    ('vapes', 'Vapes'),
    ('edibles', 'Edibles'),
    ('concentrates', 'Concentrates'),
    ('tinctures', 'Tinctures'),
    ('topicals', 'Topicals'),
    ('accessories', 'Accessories'),
    ('beverages', 'Beverages'),
    ('capsules', 'Capsules'),
    ('seeds', 'Seeds'),
    ('clones', 'Clones'),
    ('cbd', 'CBD'),
    ('merch', 'Merch'),
]


class PushBanner(models.Model):
    _name = 'mint.push.banner'
    _description = 'Push Banner'
    _order = 'sequence, id'

    name = fields.Char(required=True, string='Internal Name')
    slot = fields.Selection(
        [('interstitial', 'Interstitial'), ('slot', 'Slot'), ('popup', 'Popup')],
        required=True,
        string='Slot Type',
    )
    category = fields.Selection(CATEGORY_SELECTION, string='Category')
    sequence = fields.Integer(default=10)
    title = fields.Char(string='Headline')
    subtitle = fields.Text(string='Subtitle')
    image = fields.Image(string='Image', max_width=2048, max_height=1024)
    image_url = fields.Char(string='External Image URL', help='Takes precedence over uploaded image')
    link_url = fields.Char(string='Link URL')
    link_label = fields.Char(string='Link Label', default='Shop Now')
    background_color = fields.Char(string='Background Color', help='CSS color value')
    is_active = fields.Boolean(default=True, string='Active')
    active_from = fields.Date(string='Active From')
    active_until = fields.Date(string='Active Until')
    store_ids = fields.Many2many('res.company', string='Stores', help='Leave empty for all stores')

    def _to_api_dict(self):
        """Serialize to the flat Banner dict expected by the Dutchie SDK frontend."""
        self.ensure_one()
        if self.image_url:
            img = self.image_url
        elif self.image:
            img = f'/web/image/mint.push.banner/{self.id}/image'
        else:
            img = ''
        return {
            'id': self.id,
            'name': self.name,
            'slot': self.slot,
            'category': self.category or '',
            'sequence': self.sequence,
            'title': self.title or '',
            'subtitle': self.subtitle or '',
            'image': img,
            'link_url': self.link_url or '',
            'link_label': self.link_label or 'Shop Now',
            'background_color': self.background_color or '',
        }
