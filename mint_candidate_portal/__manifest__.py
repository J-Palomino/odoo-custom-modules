{
    "name": "Mint Candidate Portal",
    "version": "19.0.1.0.0",
    "category": "Human Resources/Recruitment",
    "summary": "Login-gated apply flow + /my/applications portal for Mint Cannabis candidates",
    "description": """
For the themintcannabis.com careers site (website id=9).

Behavior:
  * /jobs/apply/<job> (and /careers/apply/<job> via mint_career) require a
    logged-in user. Anonymous visitors are redirected to /web/signup with the
    apply URL preserved as the post-signup redirect.
  * On hr.applicant create, the linked res.partner is tagged with the
    "Applicant" partner category (res.partner.category).
  * Logged-in candidates see /my/applications with their own hr.applicant
    records, current stage, and the mail.thread conversation with HR.

Distinct from mint_recruitment_portal which serves HR users at /my/jobs and
/my/applicants. This module is for the candidate side.
""",
    "author": "Mint Cannabis",
    "license": "LGPL-3",
    "depends": [
        "hr_recruitment",
        "website_hr_recruitment",
        "portal",
        "auth_signup",
        "mail",
    ],
    "data": [
        "data/partner_category.xml",
        "views/portal_templates.xml",
    ],
    "installable": True,
    "auto_install": False,
    "application": False,
}
