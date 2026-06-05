from . import models
from . import controllers
from . import report
from . import wizard
from . import migration_helpers  # noqa: F401  — importable as odoo.addons.mint_command_center.migration_helpers
from .hooks import post_init_hook
