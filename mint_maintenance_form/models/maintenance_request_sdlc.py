"""SDLC pipeline notebook tabs on Engineering maintenance requests.

Mirrors the project.task SDLC tabs (mint_command_center) onto maintenance.request
so Engineering-team tickets carry the same Functional Analysis … PR & Deploy tabs.
The SDLC skills write them via ``task_append_section(..., model='maintenance.request')``.
Rendered (scoped to the Engineering team) by views/maintenance_request_sdlc_views.xml.

Field names kept as ``x_sdlc_*`` to adopt the manual fields created live 2026-06-10.
"""
from odoo import fields, models


class MaintenanceRequestSdlc(models.Model):
    _inherit = 'maintenance.request'

    x_sdlc_functional_analysis = fields.Html(string='Functional Analysis')
    x_sdlc_research = fields.Html(string='Research')
    x_sdlc_implementation_plan = fields.Html(string='Implementation Plan')
    x_sdlc_changelog = fields.Html(string='Changelog')
    x_sdlc_test_plan = fields.Html(string='Test Plan')
    x_sdlc_qa_report = fields.Html(string='QA Report')
    x_sdlc_pr_deploy = fields.Html(string='PR & Deploy')
