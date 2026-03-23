{
    'name': 'Mint PostHog Analytics',
    'version': '19.0.1.0.0',
    'category': 'Tools',
    'summary': 'PostHog error tracking and session recording for Odoo backend',
    'description': """
        Injects PostHog into the Odoo backend (web client) to capture:
          - JavaScript errors and unhandled promise rejections
          - RPC/API errors (failed JSON-RPC calls)
          - Session recordings for debugging
          - Page navigation and user identification

        Uses the same PostHog project as the MintDeals frontend.
        Admins are identified by their Odoo login for easy filtering.
    """,
    'author': 'Mint Dispensaries',
    'website': 'https://mintdispensaries.com',
    'license': 'LGPL-3',
    'depends': ['web'],
    'assets': {
        'web.assets_backend': [
            'mint_posthog/static/src/posthog_boot.js',
        ],
    },
    'installable': True,
    'auto_install': False,
}
