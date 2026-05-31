{
    'name': 'Mint Flipbook',
    'version': '19.0.1.0.0',
    'category': 'Marketing',
    'summary': 'Build shareable PDF + page-flip flipbooks of vendor offerings',
    'description': """
        Mint Flipbook — lets the marketing team assemble free-form pages
        (vendor sell-sheets as PDFs or images) into a single flipbook.

        Phase 1 (this release): data model + backend UI, nested under the
        "Mint Marketing" app. Marketing can create flipbook records, upload
        and reorder pages, and publish.

        Later phases:
          - Phase 2: "Generate PDF" merges all pages into one downloadable PDF.
          - Phase 3: public page-flip viewer at /flipbook/<access_token>.
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
        'views/mint_flipbook_views.xml',
        'views/menu.xml',
    ],
    'installable': True,
    'auto_install': False,
    'application': False,
}
