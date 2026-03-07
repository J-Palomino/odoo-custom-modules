{
    'name': 'Mint Marketing',
    'version': '19.0.2.3.0',
    'category': 'Marketing',
    'summary': 'Marketing Platform for Mint Cannabis',
    'description': """
        Mint Marketing — internal operations platform for managing
        55 stores across AZ, FL, MI, NV, MO, IL.

        Core models:
          - PTL Calendar (Product Timeline): Daily promotional schedules per store
          - Hot Box: Quick promotional deals
          - Brand Rankings: Brand performance tracking per store/period
          - Push Notifications: Web Push to subscribed PWA users
          - Push Banners: Interstitial, slot, and popup banners for Dutchie SDK

        Features:
          - Multi-company isolation (deals per store)
          - Calendar, kanban, list, and form views
          - Security groups with role-based access
          - Mail thread integration for collaboration
          - PWA push notification composer and subscriber management
          - Banner management with REST API for SDK frontend
    """,
    'author': 'Mint Dispensaries',
    'website': 'https://mintdispensaries.com',
    'license': 'LGPL-3',
    'depends': [
        'mail',
        'mint_push',
    ],
    'external_dependencies': {
        'python': ['pywebpush'],
    },
    'data': [
        'security/security_groups.xml',
        'security/ir.model.access.csv',
        'security/record_rules.xml',
        'views/ptl_views.xml',
        'views/hotbox_views.xml',
        'views/brand_ranking_views.xml',
        'views/compliance_views.xml',
        'views/push_site_views.xml',
        'views/push_views.xml',
        'views/push_campaign_views.xml',
        'views/push_send_wizard_views.xml',
        'views/banner_views.xml',
        'views/menu.xml',
    ],
    'installable': True,
    'auto_install': False,
    'application': True,
}
