{
    'name': 'Mint My Board',
    'version': '19.0.1.0.0',
    'category': 'Productivity',
    'summary': 'A plain /my/board page: the tasks that need you, across every project',
    'description': """
        Mint My Board
        =============

        One simple page per person at /my/board, instead of a kanban spread
        across six projects. Three sections, driven by three tags this module
        ships:

          - Decision needed : tagged "Board · Decision needed" and assigned to you
          - Needs you       : tagged "Board · Needs you" and assigned to you
          - Your requests   : tagged "Board · Your request" and followed by you

        Closed and cancelled tasks drop off. Tasks are read with the viewer's
        own access rights, across all of their companies, so the backend
        company picker can't blank the page.

        Built for Doug Schwartz's SEO/site work (2026-09-24); nothing in it is
        specific to him. Tag a task to put it on someone's board.
    """,
    'author': 'Mint Dispensaries',
    'website': 'https://letsgomint.us',
    'license': 'LGPL-3',
    'depends': [
        'website',
        'project',
    ],
    'data': [
        'data/board_data.xml',
        'views/board_templates.xml',
    ],
    'installable': True,
    'application': False,
}
