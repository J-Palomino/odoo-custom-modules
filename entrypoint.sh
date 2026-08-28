#!/bin/bash
set -e

# Fix permissions on Odoo data directory (Railway volume)
# This runs as root before switching to odoo user
if [ -d /var/lib/odoo ]; then
    echo "Fixing permissions on /var/lib/odoo..."
    chown -R odoo:odoo /var/lib/odoo
    chmod -R 755 /var/lib/odoo
fi

# Use config file if ODOO_RC is set
ODOO_CONFIG_FLAG=""
if [ -n "$ODOO_RC" ] && [ -f "$ODOO_RC" ]; then
    ODOO_CONFIG_FLAG="-c $ODOO_RC"
fi

# If installing modules, do it now as odoo user
if [ -n "$ODOO_INSTALL_MODULES" ]; then
    echo "Installing modules: $ODOO_INSTALL_MODULES"
    echo "Using config: ${ODOO_RC:-default}"
    gosu odoo odoo $ODOO_CONFIG_FLAG -d "${ODOO_DATABASE:-odoo}" -i "$ODOO_INSTALL_MODULES" --stop-after-init || {
        echo "Module installation failed, but continuing startup..."
    }
fi

# If updating modules, do it now as odoo user
if [ -n "$ODOO_UPDATE_MODULES" ]; then
    echo "Updating modules: $ODOO_UPDATE_MODULES"
    echo "Using config: ${ODOO_RC:-default}"
    gosu odoo odoo $ODOO_CONFIG_FLAG -d "${ODOO_DATABASE:-odoo}" -u "$ODOO_UPDATE_MODULES" --stop-after-init || {
        echo "Module update failed, but continuing startup..."
    }
fi

# Did the upgrade actually take?
#
# The `|| continue` above is deliberate — a failed upgrade must not take the
# site down — but it makes a rolled-back upgrade indistinguishable from a good
# one: Railway reports SUCCESS, Odoo serves 200s on the OLD code, and columns
# for new fields exist anyway because the registry creates them without -u.
# On 2026-08-28 one bad view left FOUR modules un-upgraded behind a green
# deploy and nothing surfaced it.
#
# Runs as odoo (it reads odoo.conf and imports odoo to resolve manifests the
# same way the server does). Non-fatal unless ODOO_VERIFY_UPGRADE=strict.
if [ -f /verify-module-upgrade.py ]; then
    gosu odoo python3 /verify-module-upgrade.py || {
        echo "Upgrade verification reported STALE MODULES — see the banner above." >&2
        if [ "${ODOO_VERIFY_UPGRADE:-warn}" = "strict" ]; then
            echo "ODOO_VERIFY_UPGRADE=strict — refusing to start; previous deploy keeps serving." >&2
            exit 1
        fi
    }
fi

# Execute the original entrypoint or command as odoo user
exec gosu odoo "$@"
