import json
import time

from odoo import http
from odoo.http import request

# The map only changes when the hourly brand-option sync runs, and building it
# folds the whole brand catalog, so each worker keeps it for a few minutes.
_TTL = 600
_cache = {}


class SurveyBrandOptions(http.Controller):

    @http.route('/mint/survey/<int:question_id>/brand-options', type='http',
                auth='public', methods=['GET'], csrf=False)
    def brand_options(self, question_id, **kw):
        """Map of mint.brand id -> answer id for a catalog-synced choice question.

        Used by the employee sample review prefill (view 10775): the storefront
        passes the order line's brand_id and the survey page ticks the matching
        option. Only questions flagged mint_brand_catalog_options answer, and
        the payload holds nothing but record ids already visible on the page.
        """
        hit = _cache.get(question_id)
        if not hit or time.time() - hit[0] > _TTL:
            question = request.env['survey.question'].sudo().browse(question_id).exists()
            data = question._mint_brand_answer_map() if question else {}
            hit = (time.time(), data)
            _cache[question_id] = hit
        return request.make_response(
            json.dumps(hit[1]),
            headers=[('Content-Type', 'application/json'), ('Cache-Control', 'public, max-age=600')],
        )
