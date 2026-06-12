{
    'name': 'Mint Industry Trends',
    'version': '19.0.1.0.0',
    'category': 'Operations',
    'summary': 'Apify-fed industry-trend tracking for cannabis market intelligence',
    'description': """
        Mint Industry Trends — tracks cannabis-industry signals scraped via
        Apify and surfaces them next to the competitor intel in Mint Marketing.

        Models:
          - Trend Source: a configured Apify actor run (search trends, social,
            news, product launches, competitor pricing). Admin-editable.
          - Trend Term: watchlist of brands/strains/categories/topics to follow.
          - Trend Observation: time-series datapoints written by the external
            k8s collector via XML-RPC; deduped on (source, external_id).

        The scraping itself runs in k8s/monitoring/industry-trends (Apify API
        -> Odoo XML-RPC); this module only owns the data model, UI, and a daily
        housekeeping cron that refreshes rolling trend scores. Competitor
        menu/pricing scrapes flow into mint.competitor.deal (mint_command_center).
    """,
    'author': 'Mint Dispensaries',
    'website': 'https://mintdispensaries.com',
    'license': 'LGPL-3',
    'depends': [
        'mail',
        'mint_command_center',
    ],
    'data': [
        'security/ir.model.access.csv',
        'data/ir_cron_data.xml',
        'data/trend_source_data.xml',
        'views/trend_views.xml',
        'views/menu.xml',
    ],
    'installable': True,
    'auto_install': False,
    'application': False,
}
