{
    "name": "Mint Service Request Forms",
    "version": "19.0.9.10.0",
    "category": "Maintenance",
    "summary": "Website forms for Mint Technology, Facilities, Graphics, and Vendor service requests",
    "author": "Mint Cannabis",
    "license": "LGPL-3",
    # base_automation is required by data/maintenance_region_data.xml, which
    # creates base.automation records. Production already had it installed so
    # the missing declaration never surfaced there, but without it any fresh
    # install fails with KeyError: 'base.automation'.
    "depends": ["base_automation", "maintenance", "website", "website_slides"],
    "data": [
        "security/equipment_rules.xml",
        "data/cron.xml",
        "data/facilities_data.xml",
        "data/graphics_data.xml",
        "data/engineering_data.xml",
        "data/maintenance_region_data.xml",
        "views/maintenance_request_views.xml",
        "views/maintenance_request_sdlc_views.xml",
        "views/templates.xml",
    ],
    "installable": True,
    "auto_install": False,
    "application": False,
    "post_init_hook": "post_init_hook",
}
