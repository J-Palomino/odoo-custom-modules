from odoo import api, models


class HrApplicant(models.Model):
    _inherit = "hr.applicant"

    @api.model_create_multi
    def create(self, vals_list):
        applicants = super().create(vals_list)
        tag = self.env.ref(
            "mint_candidate_portal.partner_category_applicant",
            raise_if_not_found=False,
        )
        if not tag:
            return applicants
        for applicant in applicants:
            partner = applicant.partner_id
            if partner and tag not in partner.category_id:
                partner.sudo().write({"category_id": [(4, tag.id)]})
        return applicants
