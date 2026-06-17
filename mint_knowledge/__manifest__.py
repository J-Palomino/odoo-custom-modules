{
    'name': 'Mint Knowledge Base',
    'version': '19.0.3.0.0',
    'category': 'Productivity/Knowledge',
    'summary': 'Lightweight internal knowledge base — categorized rich-text '
               'wiki pages with reader / manager access control',
    'description': """
Mint Knowledge Base
===================

A small, Odoo-19-native wiki for internal documentation (e.g. an IT knowledge
base). Replaces the OCA ``document_page`` module, which is not installable on
Odoo 19.

Models
------
* ``mint.knowledge.category`` — hierarchical categories (parent/child tree).
* ``mint.knowledge.page`` — rich-text (html_editor) pages filed under a category.

Security
--------
* **Knowledge Base / Reader** (``group_mint_knowledge_user``) — read-only.
* **Knowledge Base / Manager** (``group_mint_knowledge_manager``) — full CRUD;
  implies Reader.

Grant the Manager group to whoever curates the knowledge base, then create a
top-level category per domain (e.g. "IT").
""",
    'author': 'Mint Dispensaries',
    'website': 'https://mintdispensaries.com',
    'license': 'LGPL-3',
    'depends': ['base', 'html_editor', 'website'],
    'data': [
        'security/knowledge_security.xml',
        'security/ir.model.access.csv',
        'views/knowledge_views.xml',
        'views/website_templates.xml',
    ],
    'installable': True,
    'application': True,
    'auto_install': False,
}
