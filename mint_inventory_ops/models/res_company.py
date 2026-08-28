"""res.company extension for Dutchie receiving.

`dutchie_pos_location_id` is ALSO declared in mint_command_center
(models/dutchie_discount_push.py), which is where the field conceptually
belongs — it is the same field, the same column, and Odoo merges same-named
fields declared by several modules on one model.

It is re-declared here on purpose so mint_inventory_ops does not have to depend
on mint_command_center, which would drag `crm`, `project`, `website`,
`maintenance` and `spreadsheet_dashboard` into an inventory module for the sake
of one integer. Do not "clean this up" by deleting it unless you are also
adding that dependency.
"""
from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    dutchie_pos_location_id = fields.Integer(
        string='Dutchie POS LocId',
        help='Integer POS LocId Dutchie uses for this store (e.g. Tempe = 1568, '
             'Spring Hill = 2898). Distinct from dutchie_store_id, which is the '
             'e-commerce UUID and is NOT accepted by the POS API. A store with '
             '0 here cannot receive inventory.',
    )
