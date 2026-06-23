# -*- coding: utf-8 -*-
{
    'name': 'MintDeals Survey Company Ownership',
    'version': '19.0.1.2.0',
    'category': 'Marketing/Surveys',
    'summary': 'Restrict editing of a survey to users of an owner company',
    'description': """MintDeals Survey Company Ownership
        Odoo's survey.survey model is global (not multi-company), so any Survey
        Manager can edit any survey regardless of which team owns it. This
        module adds an optional "Owner Company" (company_id) to surveys and a
        GLOBAL record rule that restricts write/unlink of a company-owned survey
        to users who are assigned to that company.

        Key behaviours:
          - Surveys with no Owner Company stay editable by any survey manager
            (unchanged default).
          - A survey whose Owner Company is set can only be edited/deleted by
            users assigned to that company. Read is left open so results stay
            visible.
          - The rule is GLOBAL (AND-combined), so it overrides the stock
            "Survey: manager: all" rule that grants managers blanket write.
          - Full system administrators (base.group_system) are exempt to
            prevent lockout, matching the convention in mint_api_v2.
          - The public answer flow runs as sudo and is unaffected — anyone with
            the link can still take the survey.
    """,
    'author': 'MintDeals',
    'website': 'https://letsgomint.us',
    'license': 'LGPL-3',
    'depends': ['survey'],
    'data': [
        'security/survey_company_security.xml',
        'views/survey_survey_views.xml',
    ],
    'assets': {
        'web.assets_frontend': [
            'mint_survey_company/static/src/css/survey_contrast.css',
        ],
    },
    'installable': True,
    'application': False,
    'auto_install': False,
}
