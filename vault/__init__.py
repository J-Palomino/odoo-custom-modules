# © 2021 Florian Kantelberg - initOS GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import logging

from . import controllers, models, wizards

_logger = logging.getLogger(__name__)

# Email addresses of users allowed to access the vault in production.
# Edit this list to grant access to additional users.
VAULT_ALLOWED_USERS = [
    "jpalomino@brightroot.com",
]


def _vault_post_init(env):
    """Assign vault access to allowed users after module install/upgrade."""
    group = env.ref("vault.group_vault_user", raise_if_not_found=False)
    if not group:
        return
    for email in VAULT_ALLOWED_USERS:
        user = env["res.users"].search([("login", "=", email)], limit=1)
        if user:
            user.write({"group_ids": [(4, group.id)]})
            _logger.info("Vault access granted to %s", email)
        else:
            _logger.warning("Vault user not found: %s", email)
