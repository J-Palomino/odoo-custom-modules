"""Which Google Sheets to mirror, and where each one lands in Odoo."""

# One entry per store schedule Mack Thomas shared on 2026-08-20.
# `company` is the matching res.company name in Odoo, or None where no
# company record exists yet (Cave Creek — verified absent 2026-08-20).
STORES = [
    {
        'store': 'Mesa',
        'sheet_id': '1NZBvspX0UMe1PLXk4_2X5b6lpWFPvnrZQHSi6WbOInQ',
        'company': 'AZ - Mesa',
    },
    {
        'store': 'East Mesa',
        'sheet_id': '1QpGOzJWoShwUNYu6MeJF4bryR8iaZd2DFLvqleW5Yl0',
        'company': 'AZ - East Mesa',
    },
    {
        'store': 'Tempe',
        'sheet_id': '1xHRFKlaGDTjfuIVIhOvEIBAgygp_2p21w6OmlMoFiUY',
        'company': 'AZ - Tempe',
    },
    {
        'store': 'Scottsdale',
        'sheet_id': '1MQ-3qy5duL9xtEG7r2ngv2bBLSHXAgX516wQIOcSGVo',
        'company': 'AZ - Scottsdale',
    },
    {
        'store': 'Northern',
        'sheet_id': '1uMNctP2ZLNXAVFk7fBGahi0pCyQRoo3d1OLjs4uATUw',
        'company': 'AZ - Northern',
    },
    {
        'store': '75th Ave',
        'sheet_id': '1l1NPLb0UyJE3V-KBAZ5IjciSrxg5W13P_VQEttVSKxo',
        'company': 'AZ - 75th Ave',
    },
    {
        'store': 'Cave Creek',
        'sheet_id': '1cWIaBoYG113CC7AhukX-6BTR_U6hnM0eHjyecbCPe_c',
        'company': None,
    },
    {
        'store': 'El Mirage',
        'sheet_id': '1NuAGxbKpLcvkl3sng9RW7ZRK3DeGL77tjTPDXtuQdUE',
        'company': 'AZ - El Mirage',
    },
]

# Odoo spreadsheet.spreadsheet record name. One record per store; each synced
# week becomes a tab inside it. Keep this stable — the sync upserts by name.
#
# A full-history import goes to a separate record, so the routine rolling-window
# sync cannot overwrite the archive (upsert is by name, and the window contains
# far fewer weeks).
def odoo_name(store, archive=False):
    return f'Schedule — {store} (all weeks)' if archive else f'Schedule — {store}'


ODOO_URL = 'https://letsgomint.us'
ENV_PATH = '/Users/Keymaker/code/letsgomint-us/.env'

# Service account that the sheets must be shared with for unattended runs.
SERVICE_ACCOUNT_EMAIL = 'gbp-metrics@letsgomint-us.iam.gserviceaccount.com'
SERVICE_ACCOUNT_KEY = '~/gbp-metrics-key.json'

SCOPES = [
    'https://www.googleapis.com/auth/drive.readonly',
    'https://www.googleapis.com/auth/spreadsheets.readonly',
]
