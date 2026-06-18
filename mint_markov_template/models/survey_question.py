from odoo import api, models

# Sequence gaps so an editor can later slot answers between them.
_BEST_SEQ = 10
_ALT_SEQ = 20


class SurveyQuestion(models.Model):
    _inherit = 'survey.question'

    @api.model
    def _mint_three_answer_vals(self, title, best_answer, alternative_answer,
                                comment_label='Something Else',
                                mark_best_as_correct=True, scored=False,
                                sequence=10, description=False):
        """Return a ``create()``-ready vals dict for a single-choice question
        whose answers are:

          1. ``best_answer``         — the best suggested answer
          2. ``alternative_answer``  — an alternative viable solution
          3. ``comment_label``       — a free-text "Something Else" comment that
                                       counts as a real answer

        :param mark_best_as_correct: flag the best answer as the correct one
            (only surfaced when the parent survey uses a scoring mode).
        :param scored: when True, give the best answer a score of 1.0 so it is
            rewarded in a scored survey.
        """
        best_vals = {
            'value': best_answer,
            'sequence': _BEST_SEQ,
            'is_correct': bool(mark_best_as_correct),
        }
        if scored:
            best_vals['answer_score'] = 1.0
        return {
            'title': title,
            'description': description or False,
            'sequence': sequence,
            'question_type': 'simple_choice',
            'comments_allowed': True,
            'comment_count_as_answer': True,
            'comments_message': comment_label or 'Something Else',
            'suggested_answer_ids': [
                (0, 0, best_vals),
                (0, 0, {'value': alternative_answer, 'sequence': _ALT_SEQ}),
            ],
        }
