{
    "name": "DaisyERP Multi-Company Isolation",
    "version": "19.0.2.1.0",
    "post_init_hook": "post_init_hook",
    "category": "Technical",
    "summary": "Record rules + intercompany transactions for multi-company setups",
    "description": """
Multi-Company Isolation & Intercompany Transactions
====================================================

**Record Rules:**
Adds global ir.rule records that enforce company-level data isolation across
all major modules: Sales, Purchase, Accounting, Inventory, CRM, Projects,
Discuss, Website, and Livechat.

Users only see records belonging to their assigned companies.
Records with no company_id are visible only to Daisy+ (company 1) users.

**Intercompany Transactions:**
When enabled on a company (Settings > Companies > Intercompany), confirming
a Purchase Order whose vendor is another company in the system will
auto-generate a matching Sale Order in the vendor's company.
""",
    "author": "DaisyERP",
    "license": "LGPL-3",
    "depends": [
        "base",
        "sale",
        "purchase",
        "account",
        "stock",
        "crm",
        "project",
        "product",
        "website",
        "mail",
        "im_livechat",
        "website_slides",
    ],
    "data": [
        "security/multicompany_rules.xml",
    ],
    "installable": True,
    "auto_install": False,
}
