import logging
import threading

from odoo import models, tools
from odoo.modules.registry import Registry

_logger = logging.getLogger(__name__)

# Custom modules deployed in /opt/extra-addons/ that need their
# controllers registered in the routing map.
EXTRA_ADDON_MODULES = [
    'mint_maintenance_form',
    'mint_api_v2',
    'mint_theme',
    'daisy_bot',
    'vault',
    'account_financial_risk',
    'daisydo_theme',
    'daisydo_livechat',
    'daisydo_agents',
    'daisydo_multicompany',
    'daisydo_webhook',
    'base_accounting_kit',
    'base_account_budget',
]


class IrHttp(models.AbstractModel):
    _inherit = 'ir.http'

    @classmethod
    def _generate_routing_rules(cls, modules, converters):
        """Ensure extra-addon modules are included in the routing whitelist."""
        registry = Registry(threading.current_thread().dbname)
        added = []
        for mod in EXTRA_ADDON_MODULES:
            if mod not in registry._init_modules:
                registry._init_modules.add(mod)
                added.append(mod)
        if added:
            _logger.warning(
                "Fixed _init_modules: added %d missing modules: %s",
                len(added), added,
            )
            # Extend the modules list passed to the rule generator
            modules = list(set(modules) | set(added))
        return super()._generate_routing_rules(modules, converters)
