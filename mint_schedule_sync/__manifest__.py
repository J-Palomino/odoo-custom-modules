{
    'name': 'Mint Schedule Sync',
    'version': '19.0.1.0.0',
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

        Reads everything through the Sheets API so the only runtime
        dependency is `requests`. Configure via ir.config_parameter:

          mint_schedule_sync.sheets           JSON list of stores
          mint_schedule_sync.client_id        OAuth client id
          mint_schedule_sync.client_secret    OAuth client secret
          mint_schedule_sync.refresh_token    OAuth refresh token (Drive scope)
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
