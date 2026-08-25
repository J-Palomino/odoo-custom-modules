{
    'name': 'Mint PostHog Analytics',
    'version': '19.0.2.0.0',
    'category': 'Tools',
    'summary': 'PostHog error tracking and session recording for Odoo backend',
    'description': """
        Injects PostHog into the Odoo backend (web client) to capture:
          - Server RPC errors, with the Python traceback, model and method
          - Session expiry (tracked separately - it is not a crash)
          - Slow RPC calls (> 10s)
          - JavaScript errors and unhandled promise rejections
          - Session recordings for debugging
          - Page navigation, per Odoo action / model / record

        Sends data to the dedicated "LetsGoMint" PostHog project,
        separate from the MintDeals storefront project. Users are identified
        by Odoo login, uid and active company, so an error can be traced to a
        specific person and store.
    """,
    'author': 'Mint Dispensaries',
    'website': 'https://mintdispensaries.com',
    'license': 'LGPL-3',
    'depends': ['web'],
    # Attaches the server-side error log handler once per worker process.
    'post_load': 'post_load',
    'assets': {
        'web.assets_backend': [
            'mint_posthog/static/src/posthog_boot.js',
        ],
    },
    'installable': True,
    'auto_install': False,
}
