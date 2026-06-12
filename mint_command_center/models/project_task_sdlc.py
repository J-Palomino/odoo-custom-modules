"""SDLC pipeline notebook tabs on engineering project tasks.

One html field per pipeline section (Functional Analysis … PR & Deploy). The
SDLC skills write them via ``task_append_section()`` (see the LGM SDLC Contract).
Rendered as scoped notebook tabs by ``views/project_task_sdlc_views.xml`` —
engineering projects only (157 Eriq / 158 Devid / 159 NiC / 91 Infrastructure).

Field names are kept as ``x_sdlc_*`` so this module *adopts* the manual fields
created live on 2026-06-10 (same column name → no new column, no data loss).
"""
from odoo import fields, models


class ProjectTaskSdlc(models.Model):
    _inherit = 'project.task'

    x_sdlc_functional_analysis = fields.Html(string='Functional Analysis')
    x_sdlc_research = fields.Html(string='Research')
    x_sdlc_implementation_plan = fields.Html(string='Implementation Plan')
    x_sdlc_changelog = fields.Html(string='Changelog')
    x_sdlc_test_plan = fields.Html(string='Test Plan')
    x_sdlc_qa_report = fields.Html(string='QA Report')
    x_sdlc_pr_deploy = fields.Html(string='PR & Deploy')
