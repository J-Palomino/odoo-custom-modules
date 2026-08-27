{
    'name': 'Mint PostHog Analytics',
    'version': '19.0.3.0.0',
    'category': 'Tools',
    'summary': 'PostHog error tracking and session recording for Odoo backend',
    'description': """
        Comprehensive error and experience logging for Odoo, in one place.

        Four capture points, because no single hook in Odoo sees everything:
          - web client: JS crashes, RPC failures, slow RPC, navigation
          - ir.http._handle_error: every exception raised serving a request,
            including ones Odoo never logs as errors (UserError -> 422,
            session expiry -> redirect)
          - ir.cron._callback: cron failures, overruns, and a heartbeat per
            run so a cron that stops running is detectable by its absence
          - root log handler: anything logged at ERROR (boot, mail queue,
            webhooks, workers) plus allowlisted below-ERROR loggers, which is
            how failed logins are captured

        Also tracks slow requests, which raise nothing and log nothing but are
        the most common form of "Odoo is broken for me".

        Sends to the dedicated "LetsGoMint" PostHog project. Client and server
        events share the distinct_id "odoo-<uid>", so a user's browser and
        server errors land on the same person.

        Server-side capture is OFF unless MINT_POSTHOG_SERVER_CAPTURE=1.
        See README.md for the full coverage matrix and configuration.
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
