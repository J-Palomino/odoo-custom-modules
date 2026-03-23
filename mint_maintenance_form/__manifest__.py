{
    "name": "Mint Service Request Forms",
    "version": "19.0.7.1.0",
    "category": "Maintenance",
    "summary": "Website forms for Engineering, Facilities, Graphics, and Dutchie service requests",
    "author": "Mint Cannabis",
    "license": "LGPL-3",
    "depends": ["maintenance", "website"],
    "data": [
        "security/equipment_rules.xml",
        "data/facilities_data.xml",
        "data/graphics_data.xml",
        "views/maintenance_request_views.xml",
        "views/templates.xml",
    ],
    "installable": True,
    "auto_install": False,
    "application": False,
}
