# -*- coding: utf-8 -*-
from odoo import fields, models


class SurveySurvey(models.Model):
    _inherit = 'survey.survey'

    company_id = fields.Many2one(
        'res.company',
        string='Owner Company',
        index=True,
        help="Company that owns this survey. When set, only users assigned to "
             "this company (or full system administrators) may edit or delete "
             "the survey. Leave empty to keep it editable by any survey "
             "manager. The public answer flow is unaffected — anyone with the "
             "link can still respond.",
    )
