# -*- coding: utf-8 -*-
{
    'name': 'MintDeals Link Tracker QR',
    'version': '19.0.1.1.0',
    'category': 'Marketing',
    'summary': 'Generate a downloadable QR code for every tracked short link',
    'description': """MintDeals Link Tracker QR
        Extends Odoo's native Link Tracker (link.tracker) so every tracked
        short URL (the /r/<code> links under Website > Link Tracker) renders a
        scannable QR code on the form, plus a one-click button to download a
        high-resolution PNG. QR rendering reuses Odoo's built-in barcode engine
        (ir.actions.report.barcode), so no extra Python dependency is needed.
    """,
    'author': 'MintDeals',
    'website': 'https://letsgomint.us',
    'license': 'LGPL-3',
    # mass_mailing/project/website own the groups that keep edit-all on links
    # (security/link_tracker_security.xml).
    'depends': ['link_tracker', 'web', 'mass_mailing', 'project', 'website'],
    'data': [
        'security/ir.model.access.csv',
        'security/link_tracker_security.xml',
        'views/link_tracker_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
