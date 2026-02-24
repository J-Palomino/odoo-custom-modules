import logging
import threading

from odoo import models
from odoo.modules.registry import Registry

_logger = logging.getLogger(__name__)

# Custom modules deployed in /opt/extra-addons/ that have web controllers.
# Only include modules that are guaranteed to exist on disk at runtime
# (i.e., NOT removed by fix-config.sh's broken-modules cleanup).
EXTRA_CONTROLLER_MODULES = [
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
    # NOTE: avancir_inventory is removed from /opt/extra-addons/ at boot,
    # so we must NOT add it here or the asset pipeline breaks.
]


class IrHttp(models.AbstractModel):
    _inherit = 'ir.http'

    def _generate_routing_rules(self, modules, converters):
        """Ensure extra-addon modules are included in the routing whitelist.

        Custom modules from /opt/extra-addons/ may not appear in
        registry._init_modules after certain deployment scenarios.
        This override adds them so their controllers' routes are
        generated in the routing map.
        """
        registry = Registry(threading.current_thread().dbname)
        added = []
        for mod in EXTRA_CONTROLLER_MODULES:
            if mod not in registry._init_modules:
                registry._init_modules.add(mod)
                added.append(mod)
        if added:
            _logger.warning(
                "Fixed _init_modules: added %d missing modules: %s",
                len(added), added,
            )
            modules = list(set(modules) | set(added))
        return super()._generate_routing_rules(modules, converters)
