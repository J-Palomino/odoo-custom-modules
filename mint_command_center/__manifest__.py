{
    'name': 'Mint Command Center',
    'version': '19.0.6.0.0',
    'category': 'Operations',
    'summary': 'Centralized operations dashboard for Mint Cannabis',
    'description': """
        Mint Command Center — internal operations platform for managing
        55 stores across AZ, FL, MI, NV, MO, IL.

        Core models:
          - PTL Calendar (Product Timeline): Daily promotional schedules per market
          - Deal Submissions: Vendor deal submission inbox with approval workflow
          - Brand Calendar: Scheduled brand promotion slots
          - Hot Box: Quick promotional deals
          - Brand Rankings: Brand performance tracking per store/period
          - Compliance Variances: Regulatory approval tracker
          - Stock Check: Inventory verification against Dutchie API
          - Placements: Paid placement catalog, inventory, bookings & revenue
          - Billboards: Vendor/board/creative catalog and weekly schedules
          - Advertising Vendors: res.partner extension tagging paid-media partners
          - Competitors: Competitive intel with haversine nearest-Mint + Google Places lookup
          - Ambassadors: Brand ambassadors and shift scheduling

        Features:
          - Multi-market PTL calendars (AZ, NV, FL, MI, MO, IL)
          - Deal approval workflow (pending → approved/rejected → live → expired)
          - Vendor deal submission form (/promos, /vendor-deals)
          - Brand calendar with add-to-PTL workflow
          - Stock check wizard with majority-rule logic
          - Multi-company isolation (deals per store)
          - Calendar, kanban, list, and form views
          - Security groups with role-based access
          - Mail thread integration for collaboration
          - Webhook sync to inventory service (Redis)
    """,
    'author': 'Mint Dispensaries',
    'website': 'https://mintdispensaries.com',
    'license': 'LGPL-3',
    'depends': [
        'mail',
        'bus',
        'website',
        'crm',
        'report_xlsx',
        'spreadsheet_dashboard',
        'mint_push',
        'mint_banner',
        'mint_api_v2',
    ],
    'assets': {
        'web.assets_backend': [
            'mint_command_center/static/src/outdated_page_watcher_patch.js',
            'mint_command_center/static/src/ptl_calendar/ptl_calendar.esm.js',
            'mint_command_center/static/src/ptl_calendar/ptl_calendar.xml',
            'mint_command_center/static/src/ptl_calendar/ptl_calendar.scss',
            'mint_command_center/static/src/ptl_day_grid/ptl_day_grid.esm.js',
            'mint_command_center/static/src/ptl_day_grid/ptl_day_grid.xml',
            'mint_command_center/static/src/ptl_day_grid/ptl_day_grid.scss',
        ],
        'web.assets_frontend_lazy': [
            'mint_command_center/static/src/outdated_page_watcher_patch.js',
        ],
    },
    'data': [
        'security/security_groups.xml',
        'security/ir.model.access.csv',
        'security/record_rules.xml',
        'data/ptl_cron_data.xml',
        'data/placement_data.xml',
        'data/billboard_data.xml',
        'views/ptl_views.xml',
        'views/deal_submission_views.xml',
        'views/brand_calendar_views.xml',
        'views/national_promo_views.xml',
        'views/crm_lead_ext_views.xml',
        'views/hotbox_views.xml',
        'views/brand_ranking_views.xml',
        'views/compliance_views.xml',
        'views/push_views.xml',
        'views/push_site_views.xml',
        'views/push_send_wizard_views.xml',
        'views/push_campaign_views.xml',
        'views/banner_views.xml',
        'views/deal_reject_wizard_views.xml',
        'views/stock_check_wizard_views.xml',
        'views/text_deals_wizard_views.xml',
        'views/force_approve_wizard_views.xml',
        'views/revoke_wizard_views.xml',
        'views/bulk_revoke_wizard_views.xml',
        'views/conflict_resolution_wizard_views.xml',
        'views/brand_reorder_wizard_views.xml',
        'views/vendor_submission_templates.xml',
        'views/mint_brand_views.xml',
        'views/product_template_ext_views.xml',
        'views/placement_views.xml',
        'views/billboard_views.xml',
        'views/advertising_vendor_views.xml',
        'views/competitor_views.xml',
        'views/ambassador_views.xml',
        'views/menu.xml',
        'reports/ptl_calendar_reports.xml',
        'reports/ptl_calendar_pdf_template.xml',
        'data/spreadsheet_dashboards.xml',
        'views/dutchie_push_log_views.xml',
    ],
    'post_init_hook': 'post_init_hook',
    'installable': True,
    'auto_install': False,
    'application': True,
}
