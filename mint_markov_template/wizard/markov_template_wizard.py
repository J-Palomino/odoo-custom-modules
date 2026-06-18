from odoo import _, api, fields, models
from odoo.exceptions import UserError


class MintSurveyTemplateWizard(models.TransientModel):
    _name = 'mint.markov.template.wizard'
    _description = 'Generate a Markov Survey'

    title = fields.Char(
        string='Survey Title', required=True,
        help='Title of the survey to generate.')
    description = fields.Html(
        string='Description',
        help='Optional introduction shown on the survey landing page.')
    target_survey_id = fields.Many2one(
        'survey.survey', string='Append to Existing Survey',
        help='Leave empty to create a brand-new survey. Set this to add the '
             '3-answer questions to an existing survey instead.')

    comment_label = fields.Char(
        string='"Something Else" Label', required=True, default='Something Else',
        help='Prompt shown above the free-text box that captures the third, '
             'open-ended answer on every question.')
    mark_best_as_correct = fields.Boolean(
        string='Mark Best Answer as Correct', default=True,
        help='Flag the best suggested answer as the correct one. Only visible '
             'to respondents/reporting when the survey uses a scoring mode.')
    scored = fields.Boolean(
        string='Score the Survey', default=False,
        help='Turn the survey into a scored survey and award 1 point for the '
             'best suggested answer on each question.')

    questions_layout = fields.Selection(
        selection=[
            ('page_per_question', 'One page per question'),
            ('page_per_section', 'One page per section'),
            ('one_page', 'One page with all the questions'),
        ],
        string='Pagination', default='page_per_question', required=True)
    access_mode = fields.Selection(
        selection=[
            ('public', 'Anyone with the link'),
            ('token', 'Invited people only'),
        ],
        string='Access Mode', default='public', required=True)

    line_ids = fields.One2many(
        'mint.markov.template.wizard.line', 'wizard_id', string='Questions')

    def _prepare_survey_vals(self):
        self.ensure_one()
        return {
            'title': self.title,
            'description': self.description or False,
            'questions_layout': self.questions_layout,
            'access_mode': self.access_mode,
            'scoring_type': 'scoring_without_answers' if self.scored else 'no_scoring',
        }

    def action_generate(self):
        self.ensure_one()
        if not self.line_ids:
            raise UserError(_('Add at least one question before generating.'))

        Question = self.env['survey.question']
        survey = self.target_survey_id
        if survey:
            if self.scored and survey.scoring_type == 'no_scoring':
                survey.scoring_type = 'scoring_without_answers'
            # Start sequencing after the survey's existing questions.
            base_seq = (max(survey.question_ids.mapped('sequence') or [0])) + 10
        else:
            survey = self.env['survey.survey'].create(self._prepare_survey_vals())
            base_seq = 10

        for offset, line in enumerate(self.line_ids):
            vals = Question._mint_three_answer_vals(
                title=line.title,
                best_answer=line.best_answer,
                alternative_answer=line.alternative_answer,
                comment_label=self.comment_label,
                mark_best_as_correct=self.mark_best_as_correct,
                scored=self.scored,
                sequence=base_seq + offset * 10,
                description=line.description,
            )
            vals['survey_id'] = survey.id
            Question.create(vals)

        return {
            'type': 'ir.actions.act_window',
            'name': _('Survey'),
            'res_model': 'survey.survey',
            'res_id': survey.id,
            'view_mode': 'form',
            'target': 'current',
        }


class MintSurveyTemplateWizardLine(models.TransientModel):
    _name = 'mint.markov.template.wizard.line'
    _description = '3-Answer Survey Question Line'
    _order = 'sequence, id'

    wizard_id = fields.Many2one(
        'mint.markov.template.wizard', required=True, ondelete='cascade')
    sequence = fields.Integer(default=10)
    title = fields.Char(string='Question', required=True)
    description = fields.Html(
        string='Help Text',
        help='Optional clarification shown under the question.')
    best_answer = fields.Char(
        string='Best Suggested Answer', required=True)
    alternative_answer = fields.Char(
        string='Alternative Viable Solution', required=True)
