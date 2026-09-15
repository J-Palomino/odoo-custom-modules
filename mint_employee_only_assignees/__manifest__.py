{
    'name': 'Mint — Employee-Only Assignees',
    'version': '19.0.1.0.0',
    'category': 'Productivity',
    'summary': 'Restrict task/activity assignee dropdowns to linked hr.employee users',
    'description': """
Employee-only assignee dropdowns
================================

Only users linked to an ``hr.employee`` record may be picked as an assignee on
tasks, activities, projects and maintenance requests.

Why a stored flag instead of a domain on ``employee_ids``
---------------------------------------------------------
``res.users.employee_ids`` is scoped to the **active company**. On this database
(71 companies) that one2many sees only the ~120 employees of "Mint Cannabis" and
misses the ~91 who belong to per-store companies (AZ - Tempe, Mint Happy Valley,
AZ - East Mesa, ...). A domain of ``[('employee_ids','!=',False)]`` therefore looks
correct when tested as an admin on the parent company and silently drops every
store employee from the dropdown.

``res.users.employee`` (core) is a non-stored boolean, so it cannot be searched
and cannot drive a domain either.

This module adds ``res.users.mint_is_employee`` — stored, indexed, and computed
**cross-company via sudo** — and points the assignee fields at it.

Scope
-----
Fields re-domained to ``[('mint_is_employee','=',True),('active','=',True)]``:

* ``mail.activity.user_id``        — "Assigned to" on Schedule Activity / To-Do
* ``project.task.user_ids``        — task Assignees
* ``project.project.user_id``      — Project Manager
* ``maintenance.request.user_id``  — Technician

Deliberately NOT changed: ``crm.lead.user_id`` and ``account.move.user_id``
(Salesperson). Those are sales-role fields, not task assignment, and narrowing
them to HR employees could break sales ops. They currently have no ``share``
filter and so DO offer portal customers — worth fixing separately.

Operational note
----------------
A field ``domain`` is a UI filter, not a write constraint. Existing assignments
are untouched and keep rendering, and programmatic assignment (XML-RPC, ORM,
automation) is unaffected — the excluded users simply stop being *selectable*.

At time of writing this removes 104 users from the dropdowns: 65 corporate humans
with no HR record and 36 bot/service accounts. 98 of them hold 143 existing task
assignments. Create ``hr.employee`` records for anyone who should stay
selectable; the flag recomputes automatically on employee create/write/unlink.
    """,
    'author': 'Mint Cannabis',
    'website': 'https://letsgomint.com',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'mail',
        'hr',
        'project',
        'maintenance',
        # Not used for code, but required for LOAD ORDER: mint_maintenance_form
        # also redefines maintenance.request.user_id (narrowing it to internal
        # users). Whichever module loads last wins the domain, so we must load
        # after it or our employees-only rule is silently overwritten.
        'mint_maintenance_form',
    ],
    'data': [],
    'post_init_hook': 'post_init_hook',
    'installable': True,
    'application': False,
    'auto_install': False,
}
