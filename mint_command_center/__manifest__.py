{
    'name': 'Mint Command Center',
    'version': '19.0.1.6.0',
    'category': 'Operations',
    'summary': 'Centralized operations dashboard for Mint Cannabis',
    'description': """
        Mint Command Center — internal operations platform for managing
        55 stores across AZ, FL, MI, NV, MO, IL.

        Core models:
          - PTL Calendar (Product Timeline): Daily promotional schedules per store
          - Hot Box: Quick promotional deals
          - Brand Rankings: Brand performance tracking per store/period
          - Compliance Variances: Regulatory submission tracking
          - Push Notifications: Compose and send web push notifications

        Features:
          - Multi-company isolation (deals per store)
          - Calendar, kanban, list, and form views
          - Security groups with role-based access
          - Mail thread integration for collaboration
          - Push notification composer with URL templates
    """,
    'author': 'Mint Dispensaries',
    'website': 'https://mintdispensaries.com',
    'license': 'LGPL-3',
    'depends': [
        'mail',
        'mint_push',
    ],
    'data': [
        'security/security_groups.xml',
        'security/ir.model.access.csv',
        'security/record_rules.xml',
        'views/push_send_wizard_views.xml',
        'views/push_campaign_views.xml',
        'views/ptl_views.xml',
        'views/hotbox_views.xml',
        'views/brand_ranking_views.xml',
        'views/compliance_views.xml',
        'views/menu.xml',
    ],
    'installable': True,
    'auto_install': False,
    'application': True,
}
