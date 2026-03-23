{
    'name': 'Mint Redis Session Store',
    'version': '19.0.1.0.0',
    'category': 'Technical',
    'summary': 'Store HTTP sessions in Redis instead of filesystem',
    'description': """
        Replaces Odoo's default FilesystemSessionStore with a Redis-backed
        store so sessions survive worker recycling and deploys.

        Configure via env vars:
          REDIS_SESSION_URL  — full URL (redis://host:port/db)
    """,
    'author': 'Mint Dispensaries',
    'website': 'https://letsgomint.us',
    'license': 'LGPL-3',
    'depends': ['base'],
    'external_dependencies': {
        'python': ['redis'],
    },
    'installable': True,
    'auto_install': False,
    'application': False,
}
