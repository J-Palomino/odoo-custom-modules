# © 2021 Florian Kantelberg - initOS GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

{
    "name": "Vault",
    "summary": "Password vault integration in Odoo",
    "license": "AGPL-3",
    "version": "19.0.1.1.0",
    "website": "https://github.com/OCA/server-auth",
    "application": True,
    "author": "initOS GmbH, Odoo Community Association (OCA)",
    "category": "Vault",
    "depends": ["base_setup", "web"],
    # Grant pilot users (VAULT_ALLOWED_USERS in __init__.py) the vault group on
    # a fresh install. Without this hook, the allowlist only runs via the
    # 19.0.1.1.0 post-migrate (upgrade path), so a first-time -i install would
    # grant nobody access — even the allow-listed user.
    "post_init_hook": "_vault_post_init",
    "data": [
        "security/vault_security.xml",
        "security/ir.model.access.csv",
        "security/ir_rule.xml",
        "views/res_config_settings_views.xml",
        "views/res_users_views.xml",
        "views/vault_entry_views.xml",
        "views/vault_field_views.xml",
        "views/vault_file_views.xml",
        "views/vault_log_views.xml",
        "views/vault_inbox_views.xml",
        "views/vault_right_views.xml",
        "views/vault_views.xml",
        "views/menuitems.xml",
        "views/templates.xml",
        "wizards/vault_export_wizard.xml",
        "wizards/vault_import_wizard.xml",
        "wizards/vault_send_wizard.xml",
        "wizards/vault_store_wizard.xml",
    ],
    "assets": {
        "vault.assets_frontend": [
            "vault/static/src/common/*.js",
            "vault/static/src/frontend/*.js",
        ],
        # web.assets_backend intentionally left out — see below.
        #
        # This module is UNINSTALLED on production, but Odoo was still pulling
        # these files into web.assets_backend, so every staff member's browser
        # loaded vault's JS on every backend page. That JS calls
        # `/vault/keys/get` on startup; with the module uninstalled the
        # controller does not serve that route, so Odoo's website returns a
        # 33 KB HTML 404, the web client tries to JSON.parse it, fails, and
        # raises ConnectionLostError — surfacing to staff as
        # "Connection couldn't be established or was interrupted".
        #
        # Measured 2026-08-28: ~29 occurrences in a week across 6 named staff,
        # firing on every action (discuss, all-tasks, spreadsheet). It was the
        # single most frequent Odoo error, and invisible until backend
        # telemetry was fixed. Verified the module is genuinely uninstalled,
        # has no dependents, and owns zero ir.model.data rows; and that of the
        # 118 modules contributing to the backend bundle it was the ONLY
        # uninstalled one. Clearing the cached bundle attachments did not help
        # — the rebuilt bundle still contained it — so the manifest is the
        # only thing keeping these files in the graph.
        #
        # If vault is ever reinstalled, restore this block; the backend UI
        # needs it. Nothing else references these paths.
        "web.assets_unit_tests": [
            "vault/static/tests/**/*.js",
        ],
    },
}
