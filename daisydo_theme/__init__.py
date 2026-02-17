# DaisyDo Theme
import json
import logging
import os

from . import controllers
from . import models

_logger = logging.getLogger(__name__)


def _ensure_page_layouts(env):
    """Wrap website pages missing website.layout so CSS/JS/navbar loads."""
    env.cr.execute("""
        UPDATE ir_ui_view SET arch_db = jsonb_build_object(
            'en_US',
            '<t t-call="website.layout">' || chr(10) ||
            '  <t t-set="pageName" t-value="''page''"/>' || chr(10) ||
            '  ' || (arch_db ->> 'en_US') || chr(10) ||
            '</t>'
        )
        WHERE id IN (
            SELECT v.id FROM ir_ui_view v
            JOIN website_page wp ON wp.view_id = v.id
            WHERE jsonb_typeof(v.arch_db) = 'object'
              AND (v.arch_db ->> 'en_US') NOT LIKE '%%website.layout%%'
              AND (v.arch_db ->> 'en_US') IS NOT NULL
              AND length(v.arch_db ->> 'en_US') > 10
        )
    """)
    _logger.info("Page layouts: %d views wrapped with website.layout", env.cr.rowcount)


def _rebrand_odoobot(env):
    """Rename OdooBot to the branded bot name on the partner record for uid=1."""
    bot_name = os.environ.get('BRAND_BOT_NAME', 'DaisyBot')
    bot_email = os.environ.get('BRAND_BOT_EMAIL', 'daisybot@daisyerp.com')
    try:
        partner = env.ref('base.partner_root', raise_if_not_found=False)
        if partner:
            partner.write({'name': bot_name, 'email': bot_email})
            _logger.info("Rebranded OdooBot → %s (partner %d)", bot_name, partner.id)
    except Exception as e:
        _logger.warning("Could not rebrand OdooBot: %s", e)


def _set_company_branding(env):
    """Set company/website names and configure base URL from env vars."""
    brand_name = os.environ.get('BRAND_NAME', 'Daisy+')
    brand_domain = os.environ.get('BRAND_DOMAIN', '')

    company = env['res.company'].browse(1)
    if company.exists() and company.name in ('My Company', 'YourCompany', 'Odoo'):
        company.write({'name': brand_name})
        _logger.info("Company 1 renamed to %s", brand_name)

    website = env['website'].browse(1)
    if website.exists():
        vals = {'name': brand_name}
        if brand_domain:
            vals['domain'] = brand_domain
        website.write(vals)
        _logger.info("Website 1 set to %s / %s", brand_name, brand_domain or '(no domain)')

    if brand_domain:
        ICP = env['ir.config_parameter'].sudo()
        ICP.set_param('web.base.url', brand_domain)
        ICP.set_param('web.base.url.freeze', 'True')
        _logger.info("web.base.url set to %s (frozen)", brand_domain)


def _deactivate_brand_promotion(env):
    """Deactivate Odoo brand_promotion views so DaisyDo branding shows."""
    views = env['ir.ui.view'].search([
        ('key', 'in', ['website.brand_promotion', 'website_sale.brand_promotion']),
        ('active', '=', True),
    ])
    if views:
        views.write({'active': False})
        _logger.info("Deactivated %d brand_promotion views", len(views))


def _suppress_enterprise_upsell(env):
    """Suppress Enterprise expiration warnings and upsell links."""
    ICP = env['ir.config_parameter'].sudo()
    ICP.set_param('database.expiration_date', '2099-12-31')
    ICP.set_param('database.enterprise_code', 'disabled')
    # Remove publisher warranty URL (prevents "Register your subscription" nag)
    env.cr.execute(
        "DELETE FROM ir_config_parameter WHERE key = 'publisher_warranty_url'"
    )
    _logger.info("Enterprise upsell suppressed")


def _configure_ice_servers(env):
    """Set mail.ice_servers from TURN_URL/USERNAME/PASSWORD env vars."""
    turn_url = os.environ.get('TURN_URL', 'turn:coturn:3478')
    turn_user = os.environ.get('TURN_USERNAME', 'odoo')
    turn_pass = os.environ.get('TURN_PASSWORD',
                               os.environ.get('TURN_SECRET', 'odoo_turn_dev'))

    ice_servers = [
        {"urls": ["stun:stun.l.google.com:19302", "stun:stun1.l.google.com:19302"]},
        {"urls": [turn_url], "username": turn_user, "credential": turn_pass},
    ]
    ICP = env['ir.config_parameter'].sudo()
    ICP.set_param('mail.ice_servers', json.dumps(ice_servers))
    _logger.info("ICE servers configured (TURN: %s)", turn_url)


def _rebrand_views(env):
    """Replace hardcoded brand strings in XML view templates (ORM approach)."""
    brand_name = os.environ.get('BRAND_NAME', 'Daisy+')
    if brand_name == 'Daisy+':
        return  # Default — no replacement needed

    replacements = [
        ("'Grown with Daisy 🌼'", f"'Grown with {brand_name} 🌼'"),
        ("Grown with Daisy", f"Grown with {brand_name}"),
        ("<strong>Daisy</strong>", f"<strong>{brand_name}</strong>"),
        ("title or 'Daisy+'", f"title or '{brand_name}'"),
    ]
    views = env['ir.ui.view'].search([('key', 'like', 'daisydo_theme.%')])
    for view in views:
        arch = view.arch_db
        if not isinstance(arch, str):
            continue
        original = arch
        for old, new in replacements:
            arch = arch.replace(old, new)
        if arch != original:
            view.write({'arch_db': arch})
            _logger.info("Rebranded view '%s' (id=%d)", view.key, view.id)


def _rebrand_colors(env):
    """Replace hardcoded color hex values in the branding CSS view."""
    primary = os.environ.get('BRAND_PRIMARY_COLOR', '#FFD400')
    secondary = os.environ.get('BRAND_SECONDARY_COLOR', '#E5BF00')
    if primary == '#FFD400' and secondary == '#E5BF00':
        return  # Default colors — nothing to replace

    # Convert hex to RGB for rgba() values
    def hex_to_rgb(h):
        h = h.lstrip('#')
        return f"{int(h[0:2], 16)}, {int(h[2:4], 16)}, {int(h[4:6], 16)}"

    primary_rgb = hex_to_rgb(primary)

    color_replacements = [
        ('#FFD400', primary),
        ('#E5BF00', secondary),
        ('#CCAA00', secondary),
        ('#B8A000', secondary),
        ('255, 212, 0', primary_rgb),
    ]

    views = env['ir.ui.view'].search([
        ('key', 'like', 'daisydo_theme.%'),
    ])
    for view in views:
        arch = view.arch_db
        if not isinstance(arch, str):
            continue
        original = arch
        for old, new in color_replacements:
            arch = arch.replace(old, new)
        if arch != original:
            view.write({'arch_db': arch})
            _logger.info("Recolored view '%s' (id=%d)", view.key, view.id)


def _cleanup_daisydo_push(env):
    """Remove remnants of the old daisydo_push module."""
    env.cr.execute("DELETE FROM ir_config_parameter WHERE key LIKE 'daisydo_push.%%'")
    env.cr.execute("DROP TABLE IF EXISTS push_subscription CASCADE")
    env.cr.execute("""
        DELETE FROM ir_ui_view
        WHERE name = 'res.users.form.push' AND model = 'res.users'
    """)
    env.cr.execute("""
        DELETE FROM ir_model_fields
        WHERE model = 'res.users'
          AND name IN ('push_notify_discuss', 'push_notify_tasks', 'push_notify_other')
    """)
    env.cr.execute("ALTER TABLE res_users DROP COLUMN IF EXISTS push_notify_discuss")
    env.cr.execute("ALTER TABLE res_users DROP COLUMN IF EXISTS push_notify_tasks")
    env.cr.execute("ALTER TABLE res_users DROP COLUMN IF EXISTS push_notify_other")
    env.cr.execute("DELETE FROM ir_model_data WHERE module = 'daisydo_push'")
    env.cr.execute("DELETE FROM ir_model_fields WHERE model = 'push.subscription'")
    env.cr.execute(
        "UPDATE ir_module_module SET state = 'uninstalled' WHERE name = 'daisydo_push'"
    )
    _logger.info("daisydo_push cleanup complete")


def post_init_hook(env):
    """Post-install/upgrade hook: apply branding, config, and cleanup."""
    _logger.info("daisydo_theme post_init_hook starting...")
    _ensure_page_layouts(env)
    _rebrand_odoobot(env)
    _set_company_branding(env)
    _rebrand_views(env)
    _rebrand_colors(env)
    _deactivate_brand_promotion(env)
    _suppress_enterprise_upsell(env)
    _configure_ice_servers(env)
    _cleanup_daisydo_push(env)
    _logger.info("daisydo_theme post_init_hook complete")
