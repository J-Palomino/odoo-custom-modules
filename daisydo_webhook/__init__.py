import logging
import os

from . import models

_logger = logging.getLogger(__name__)


def _configure_webhook_from_env(env):
    """Read WEBHOOK_EVENT_SECRET from env and set system params if present."""
    secret = os.environ.get('WEBHOOK_EVENT_SECRET', '')
    if not secret:
        _logger.info("WEBHOOK_EVENT_SECRET not set, skipping webhook config")
        return

    ICP = env['ir.config_parameter'].sudo()
    ICP.set_param('daisydo_webhook.event_secret', secret)
    ICP.set_param('daisydo_webhook.enabled', '1')
    _logger.info("Webhook event secret configured, webhooks enabled")


def post_init_hook(env):
    """Post-install/upgrade hook: configure webhooks from environment."""
    _logger.info("daisydo_webhook post_init_hook starting...")
    _configure_webhook_from_env(env)
    _logger.info("daisydo_webhook post_init_hook complete")
