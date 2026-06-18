{
    'name': 'Markov Survey Template (3-Answer Generator)',
    'version': '19.0.1.0.0',
    'category': 'Marketing/Surveys',
    'summary': 'Generate surveys whose questions each offer a best answer, an '
               'alternative, and a free-text "Something Else" option',
    'description': """
Markov Survey Template — 3-Answer Generator
=========================================

Adds a wizard (Surveys > Generate Markov Survey) that builds a survey where
every question follows the same shape:

  1. Best suggested answer        (a chooseable option, optionally the "correct" one)
  2. Alternative viable solution  (a second chooseable option)
  3. Something Else               (a free-text comment box that counts as an answer)

Under the hood each question is a ``simple_choice`` survey question with two
``suggested_answer_ids`` plus ``comments_allowed`` + ``comment_count_as_answer``
for the free-text option. The wizard can create a brand-new survey or append
3-answer questions to an existing one.
""",
    'author': 'Mint Dispensaries',
    'website': 'https://mintdispensaries.com',
    'license': 'LGPL-3',
    'depends': ['survey'],
    'data': [
        'security/ir.model.access.csv',
        'wizard/markov_template_wizard_views.xml',
        'views/markov_template_menu.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
