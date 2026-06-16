{
    'name': 'Mint Social Publish',
    'version': '19.0.7.2.0',
    'category': 'Marketing',
    'summary': 'Push scheduled Social Media project cards to the posts.agency API',
    'description': """
Mint Social Publish
===================

Bridges the Odoo "Social Media" project board (project.task kanban) to the
posts.agency publishing API so the posts scheduled in Odoo actually reach the
social platforms.

How it works
------------
* Adds publishing fields to ``project.task`` (target posts.agency profile,
  platforms, caption, and result tracking).
* When a card enters the configured **Scheduled** stage of the configured
  Social Media project, ``write()`` validates the card (profile, platforms,
  media attachment, future deadline) and marks it ``pending``. Invalid cards
  raise a UserError and the move is blocked until they are fixed.
* An ``ir.cron`` sweeps ``pending`` (and ``failed`` within the retry budget)
  cards and forwards each to posts.agency:
    - Images  -> photo endpoint (binary upload)
    - Videos  -> video endpoint (tokenized public Odoo URL)
  Both send the profile, platform(s), title, caption and scheduled date
  (ISO-8601 UTC).
* The posts.agency ``job_id`` / error is stored back on the card. Results are
  written to fields only — NOT chatter — to avoid the known prod
  ``message_post`` "unhashable type: list" automation bug on project.task.
* A manual **Publish to posts.agency** button runs the same path on demand.

Configuration (Settings > posts.agency Publishing, or System Parameters)
------------------------------------------------------------------------
* ``mint_social_publish.api_base``           posts.agency API base URL
* ``mint_social_publish.api_key``            REQUIRED — posts.agency API key (secret; set in Settings)
* ``mint_social_publish.project_id``         (default 42 — Social Media)
* ``mint_social_publish.scheduled_stage_id`` (default 1672 — "Scheduled" stage)
* ``mint_social_publish.max_retries``        (default 3)

A **Test Connection** button on the Settings page validates the API key.

Note: video posts are fetched by posts.agency from a tokenized
``/web/content`` URL, so ``web.base.url`` must be the public host for video
publishing to work.
    """,
    'author': 'Mint Dispensaries',
    'website': 'https://mintdispensaries.com',
    'license': 'LGPL-3',
    'depends': ['project'],
    'data': [
        'security/social_publish_security.xml',
        'security/ir.model.access.csv',
        'data/ir_config_parameter.xml',
        'data/ir_cron.xml',
        'views/social_profile_views.xml',
        'views/res_config_settings_views.xml',
        'views/project_task_views.xml',
        'views/social_calendar_views.xml',
    ],
    'external_dependencies': {'python': ['requests']},
    'installable': True,
    'application': False,
}
