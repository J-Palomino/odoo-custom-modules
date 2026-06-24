{
    'name': 'Mint Decision Board',
    'version': '19.0.1.0.0',
    'category': 'Marketing',
    'summary': 'Reusable, login-gated decision boards that collect stakeholder votes',
    'description': """
        Mint Decision Board
        ===================

        A generic, data-driven way to publish a branded one-page decision
        brief to internal stakeholders and capture their vote + free-text
        input back into Odoo. No code per decision — create records.

          - mint.decision        : the topic (title, framed question, rich
                                    HTML body, state draft/open/closed).
          - mint.decision.option : the configurable choices to vote on.
          - mint.decision.vote   : one vote per user per decision (upsert),
                                    option + comment.

        Web:
          - /decisions              index of open boards
          - /decision/<slug>        the branded board + ballot + live tally
          (auth='user', website=True)

        Ships one ready-made board as data: the GBP "Dispensary" rename
        decision (data/gbp_decision_data.xml) — delete or edit freely.
    """,
    'author': 'Mint Dispensaries',
    'website': 'https://letsgomint.us',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'website',
        'mint_maintenance_form',
    ],
    'data': [
        'security/ir.model.access.csv',
        'security/record_rules.xml',
        'data/csuite_params.xml',
        'views/decision_backend.xml',
        'views/decision_templates.xml',
        'views/tickets_integration.xml',
        'data/gbp_decision_data.xml',
    ],
    'installable': True,
    'application': False,
}
