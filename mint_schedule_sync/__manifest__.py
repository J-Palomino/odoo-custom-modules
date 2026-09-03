{
    'name': 'Mint Schedule Sync',
    'version': '19.0.1.1.0',
    'category': 'Human Resources',
    'summary': 'Mirror the store schedule Google Sheets into Odoo spreadsheets',
    'description': """Mint Schedule Sync
        Pulls the per-store employee schedule Google Sheets into
        spreadsheet.spreadsheet records on a cron, one record per store with
        one tab per synced week.

        Values, merged ranges and cell styling are copied verbatim. Shifts are
        NOT parsed and hr.employee is NOT touched: this instance has no
        Planning module, and most people named on these sheets have no
        employee record.

        Reads everything through the Sheets API. Runtime deps are `requests`
        and, for the service-account path, `PyJWT` — both already in the image.
        Configure via ir.config_parameter:

          mint_schedule_sync.sheets                JSON list of stores
          mint_schedule_sync.service_account_json  SA key JSON (PREFERRED)

        or, as a fallback, a user OAuth credential:

          mint_schedule_sync.client_id        OAuth client id
          mint_schedule_sync.client_secret    OAuth client secret
          mint_schedule_sync.refresh_token    OAuth refresh token (Drive scope)

        Prefer the service account: it does not expire. The user credential
        lapsed three times in four days during development, and the OAuth
        client Odoo already had configured (google_calendar_client_id) is
        DELETED in Google Cloud — it returns Error 401 deleted_client.
    """,
    'author': 'MintDeals',
    'website': 'https://letsgomint.us',
    'license': 'LGPL-3',
    'depends': ['base'],
    'data': [
        'data/ir_cron.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
