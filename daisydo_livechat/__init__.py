import logging
import os

from . import models
from . import controllers

_logger = logging.getLogger(__name__)


def post_init_hook(env):
    """Update default livechat channel with brand env vars."""
    brand_name = os.environ.get('BRAND_NAME', 'Daisy+')
    primary_color = os.environ.get('BRAND_PRIMARY_COLOR', '#FFD400')
    channel_name = os.environ.get('LIVECHAT_CHANNEL_NAME', f'{brand_name} Support')
    button_text = os.environ.get('LIVECHAT_BUTTON_TEXT', f'Chat with {brand_name}')
    greeting = os.environ.get(
        'LIVECHAT_GREETING',
        f"Hi! \U0001f44b I'm {brand_name}, your AI assistant. How can I help you today?",
    )

    # Rebrand livechat views (ORM approach — SQL jsonb replace fails on Odoo 19)
    if brand_name != 'Daisy+':
        lc_replacements = [
            ("<strong>Daisy</strong>", f"<strong>{brand_name}</strong>"),
            ("Grown with Daisy", f"Grown with {brand_name}"),
            ("Embed Daisy+ Chat", f"Embed {brand_name} Chat"),
        ]
        views = env['ir.ui.view'].search([('key', 'like', 'daisydo_livechat.%')])
        for view in views:
            arch = view.arch_db
            if not isinstance(arch, str):
                continue
            original = arch
            for old, new in lc_replacements:
                arch = arch.replace(old, new)
            if arch != original:
                view.write({'arch_db': arch})
                _logger.info("Rebranded livechat view '%s' (id=%d)", view.key, view.id)

    channel = env.ref('daisydo_livechat.daisy_livechat_channel', raise_if_not_found=False)
    if channel:
        channel.write({
            'name': channel_name,
            'button_text': button_text,
            'auto_greet_message': greeting,
            'widget_primary_color': primary_color,
            'widget_header_text': f'{brand_name} Chat',
        })
        _logger.info("Livechat channel updated with brand: %s", brand_name)
