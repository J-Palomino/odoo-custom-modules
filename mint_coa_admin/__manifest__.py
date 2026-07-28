{
    "name": "Mint COA Admin",
    "summary": "Staff panel to browse/upload/organize the Certificates of Analysis "
               "in the Odoo DMS, served natively by Odoo so it uses the signed-in "
               "user's own session and DMS access rules.",
    "version": "19.0.1.1.0",
    "author": "Mint",
    "license": "LGPL-3",
    "category": "Document Management",
    # link_tracker: each certificate row exposes a tracked /r/<code> short link
    # to its PDF, minted on demand via link.tracker.
    "depends": ["dms", "web", "link_tracker"],
    "data": [],
    "installable": True,
    "application": False,
}
