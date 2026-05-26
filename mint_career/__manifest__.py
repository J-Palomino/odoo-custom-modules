{
    "name": "Mint Career",
    "version": "19.0.1.0.0",
    "category": "Human Resources/Recruitment",
    "summary": "Public /careers route + branding hook for the Mint Cannabis careers site",
    "description": """
Mounts the website_hr_recruitment job-listing flow at /careers in addition to /jobs.

Public routes (verified against odoo/odoo @ 19.0):
  /careers                       -> job listings
  /careers/page/<n>              -> paginated listings
  /careers/detail/<hr.job>       -> job detail (canonical)
  /careers/<hr.job>              -> job detail (slug)
  /careers/apply/<hr.job>        -> apply form

301 redirects from /jobs/* and brand templates are added in later phases
(see mint_career_theme / Phase 3 and the Phase 6 cutover).
""",
    "author": "Mint Cannabis",
    "license": "LGPL-3",
    "depends": [
        "website_hr_recruitment",
        "mint_theme",
    ],
    "data": [],
    "installable": True,
    "auto_install": False,
    "application": False,
}
