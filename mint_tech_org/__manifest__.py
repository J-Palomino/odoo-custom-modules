{
    'name': 'Mint Technology Org & RBAC',
    'version': '19.0.1.0.0',
    'category': 'Administration',
    'summary': 'Technology department access groups and role-based permissions',
    'description': """
        Mint Technology Org & RBAC — defines the Technology department's
        access-control model (groups + permissions ONLY; no new models, no
        hr.department / hr.job data).

        Two tiers, mirroring the mint_command_center pattern:

          Capability groups (never hand-assigned — composed into roles):
            - Viewer
            - Infrastructure / DevOps
            - Security Operations
            - Data Engineering
            - Software Development
            - IT Operations
            - Technology Admin (implies all six)

          Role groups (assigned to users; each implies base.group_user,
          base.group_allow_export, the relevant capability group(s), and the
          appropriate Odoo app groups for project + maintenance):
            CTO, Engineering Manager, DevOps Lead, DevOps Engineer,
            Security Lead, Security Engineer, Data Lead, Data Engineer,
            Software Lead, Full-Stack Dev, IT Lead, IT Specialist.

        Least-privilege by design: NO Accounting/Finance, HR, or
        Settings/Administration groups are granted to any role.
    """,
    'author': 'Mint Dispensaries',
    'website': 'https://mintdispensaries.com',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'mail',
        'hr',
        'project',
        'maintenance',
    ],
    'data': [
        'security/security_groups.xml',
        'security/ir.model.access.csv',
        'security/record_rules.xml',
    ],
    'installable': True,
    'auto_install': False,
    'application': False,
}
