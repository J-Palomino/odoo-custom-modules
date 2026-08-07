# -*- coding: utf-8 -*-
{
    'name': 'Mint Mail Approval Gate',
    'version': '19.0.1.0.0',
    'category': 'Technical',
    'summary': 'Hold every outgoing email for human approval before it is sent',
    'description': """Mint Mail Approval Gate
========================
Adds a human-approval step in front of outgoing email.

Every ``mail.mail`` is classified against an ordered set of rules before it may
leave the instance. Anything not explicitly auto-approved is parked in a review
queue until a member of *Mail Approver* presses Approve.

This is deliberately a SECOND, INDEPENDENT gate. It composes with, and does not
replace, ``mint_mail_whitelist``:

  * ``mint_mail_whitelist`` decides **who may receive** (recipient allowlist,
    applied at create time, fail-closed).
  * ``mint_mail_approval`` decides **whether a human approved the send**
    (applied at send time, fail-closed).

The choke point is ``mail.mail.send()``, which is the single method both the
queue cron (``process_email_queue``) and every ``force_send=True`` template
send funnel through. Gating there means no send path bypasses review.
""",
    'author': 'MintDeals',
    'website': 'https://letsgomint.us',
    'license': 'LGPL-3',
    'depends': ['mail'],
    'data': [
        'security/mail_approval_security.xml',
        'security/ir.model.access.csv',
        'data/config_params.xml',
        'data/approval_rules.xml',
        'data/ir_cron.xml',
        'views/mail_approval_rule_views.xml',
        'views/mail_mail_views.xml',
        'views/menu.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
