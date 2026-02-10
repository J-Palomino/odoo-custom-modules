{
    'name': 'Daisy Bot',
    'version': '19.0.1.3.0',
    'category': 'Discuss',
    'summary': 'AI assistant powered by Daisy+ in Odoo Discuss',
    'description': """
        Adds a Daisy AI assistant to Odoo Discuss.
        Users can DM Daisy or @mention her in channels.
        Messages are forwarded to the Daisy+ API and responses
        are posted back automatically.

        Configuration (Settings > Technical > System Parameters):
          - daisy_bot.api_url: Daisy+ prediction endpoint
          - daisy_bot.api_key: Bearer token for authentication
    """,
    'author': 'Mint Dispensaries',
    'website': 'https://mintdispensaries.com',
    'license': 'LGPL-3',
    'depends': ['mail'],
    'data': [
        'data/daisy_bot_data.xml',
    ],
    'installable': True,
    'auto_install': False,
    'application': False,
}
