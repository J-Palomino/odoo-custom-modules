{
    "name": "Mint Recruitment Portal",
    "version": "19.0.1.0.0",
    "category": "Human Resources/Recruitment",
    "summary": "Website portal for HR admins to manage job listings and review applicants",
    "author": "Mint Cannabis",
    "license": "LGPL-3",
    "depends": [
        "hr_recruitment",
        "website_hr_recruitment",
        "portal",
        "website",
    ],
    "data": [
        "security/ir.model.access.csv",
        "views/portal_templates.xml",
    ],
    "installable": True,
    "auto_install": False,
    "application": False,
}
