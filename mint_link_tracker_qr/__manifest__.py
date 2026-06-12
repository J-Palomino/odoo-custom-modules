# -*- coding: utf-8 -*-
{
    'name': 'MintDeals Link Tracker QR',
    'version': '19.0.1.0.0',
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
    'depends': ['link_tracker', 'web'],
    'data': [
        'views/link_tracker_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
