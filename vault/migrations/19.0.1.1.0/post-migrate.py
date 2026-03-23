# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import logging

_logger = logging.getLogger(__name__)

VAULT_ALLOWED_USERS = [
    "jpalomino@brightroot.com",
]


def migrate(env):
    """Grant vault access to allowed users after upgrade."""
    group = env.ref("vault.group_vault_user", raise_if_not_found=False)
    if not group:
        _logger.warning("vault.group_vault_user not found — skipping user assignment")
        return
    for email in VAULT_ALLOWED_USERS:
        user = env["res.users"].search([("login", "=", email)], limit=1)
        if user:
            user.write({"group_ids": [(4, group.id)]})
            _logger.info("Vault access granted to %s", email)
        else:
            _logger.warning("Vault user not found: %s", email)
