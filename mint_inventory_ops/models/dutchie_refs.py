"""Dutchie reference data mirrored into Odoo so the receive form can use dropdowns.

Rooms and vendors are per-LOCATION in Dutchie, and their ids are meaningless
integers — RoomId 34515, VendorId 79782. Asking a receiving clerk to type those
is how a receive lands in the wrong room (which orphans it, with no
PackageInventory rows) or against the wrong vendor (which creates a junk vendor
record). Mirroring them lets the form offer the real list, scoped to the store
being received into.

These are caches, not masters: Dutchie owns the data and a sync overwrites.
Nothing here is edited by hand, which is why the models are tiny and carry no
workflow. `_sync_from_payload` is fed by whatever already asked Dutchie for the
lists — currently the receive/validate and receive/refs responses from
mintinvsvc, which read `v2/room/get-rooms` and `vendor/get-vendors`.

Rows are keyed on (loc_id, dutchie_*_id) so a re-sync updates in place and
existing receives keep pointing at the same record.
"""
from odoo import api, fields, models


class DutchieRoom(models.Model):
    _name = 'mint.dutchie.room'
    _description = 'Dutchie Room'
    _order = 'loc_id, is_inventory_room desc, name'

    name = fields.Char(string='Room', required=True)
    loc_id = fields.Integer(
        string='Dutchie LocId', required=True, index=True,
        help='The store this room belongs to. Rooms are per-location in Dutchie.',
    )
    dutchie_room_id = fields.Integer(string='RoomId', required=True, index=True)
    is_inventory_room = fields.Boolean(
        string='Inventory Room',
        help='Dutchie InventoryRoom = yes. A receive should land in one of these; '
             'a non-inventory room (e.g. Waiting Room) will not hold packages.',
    )
    active = fields.Boolean(default=True)

    @api.depends('name', 'dutchie_room_id')
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = f'{rec.name} ({rec.dutchie_room_id})'

    @api.model
    def _sync_from_payload(self, loc_id, rooms):
        """Upsert the rooms Dutchie reports for one location.

        Returns the recordset. Rows Dutchie no longer reports are archived
        rather than deleted, so a receive that already points at one keeps
        resolving instead of showing a broken reference.
        """
        loc_id = int(loc_id or 0)
        if not loc_id:
            return self.browse()
        seen = []
        for r in rooms or []:
            rid = int(r.get('RoomId') or 0)
            if not rid:
                continue
            vals = {
                'name': r.get('RoomName') or r.get('Name') or f'Room {rid}',
                'loc_id': loc_id,
                'dutchie_room_id': rid,
                'is_inventory_room': str(r.get('InventoryRoom') or '').lower() == 'yes',
                'active': True,
            }
            rec = self.with_context(active_test=False).search(
                [('loc_id', '=', loc_id), ('dutchie_room_id', '=', rid)], limit=1)
            if rec:
                rec.write(vals)
            else:
                rec = self.create(vals)
            seen.append(rec.id)
        stale = self.with_context(active_test=False).search(
            [('loc_id', '=', loc_id), ('id', 'not in', seen)])
        stale.write({'active': False})
        return self.browse(seen)


class DutchieVendor(models.Model):
    _name = 'mint.dutchie.vendor'
    _description = 'Dutchie Vendor'
    _order = 'loc_id, name'

    name = fields.Char(string='Vendor', required=True)
    loc_id = fields.Integer(
        string='Dutchie LocId', required=True, index=True,
        help='Vendors are per-location in Dutchie; the same company can carry a '
             'different VendorId at another store.',
    )
    dutchie_vendor_id = fields.Integer(string='VendorId', required=True, index=True)
    vendor_code = fields.Char(
        string='Vendor Code (licence)',
        help="Dutchie's own code for this vendor. This is the value that goes "
             'into the manifest as from_license_number — never a state licence '
             'typed by hand, which creates a junk vendor record on receive.',
    )
    active = fields.Boolean(default=True)

    @api.depends('name', 'dutchie_vendor_id')
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = f'{rec.name} ({rec.dutchie_vendor_id})'

    @api.model
    def _sync_from_payload(self, loc_id, vendors):
        """Upsert the vendors Dutchie reports for one location.

        Duplicates are real: AZ carries two KANNABOOST rows under different
        VendorIds with different codes. Both are kept — picking the right one is
        a human judgement, and silently collapsing them would pick for them.
        """
        loc_id = int(loc_id or 0)
        if not loc_id:
            return self.browse()
        seen = []
        for v in vendors or []:
            vid = int(v.get('VendorId') or 0)
            if not vid:
                continue
            vals = {
                'name': v.get('VendorName') or f'Vendor {vid}',
                'loc_id': loc_id,
                'dutchie_vendor_id': vid,
                'vendor_code': v.get('VendorCode') or '',
                'active': True,
            }
            rec = self.with_context(active_test=False).search(
                [('loc_id', '=', loc_id), ('dutchie_vendor_id', '=', vid)], limit=1)
            if rec:
                rec.write(vals)
            else:
                rec = self.create(vals)
            seen.append(rec.id)
        stale = self.with_context(active_test=False).search(
            [('loc_id', '=', loc_id), ('id', 'not in', seen)])
        stale.write({'active': False})
        return self.browse(seen)
