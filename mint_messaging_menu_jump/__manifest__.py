{
    "name": "Mint Messaging Menu — Jump to Record",
    "version": "19.0.1.0.0",
    "summary": "Bell-dropdown inbox clicks open the source record, not the Discuss inbox list",
    "description": """
Standard Odoo 19 behavior: clicking a notification in the bell-icon dropdown
opens Discuss > Inbox (a list), then the user has to find the row again and
click into the source record.

This module patches MessagingMenu.onClickInboxMsg so that, when a notification
has a backend model + res_id (e.g. a maintenance.request assignment, a comment
on a project.task, an HR follow-up), the click opens the record's form view
directly. Discuss channel messages (msg.thread.model === 'discuss.channel')
and messages without a thread fall through to the original behavior.
    """,
    "category": "Tools",
    "author": "Mint Cannabis",
    "website": "https://letsgomint.us",
    "license": "LGPL-3",
    "depends": ["mail"],
    "assets": {
        "web.assets_backend": [
            "mint_messaging_menu_jump/static/src/js/messaging_menu_patch.js",
        ],
    },
    "installable": True,
    "application": False,
    "auto_install": True,
}
