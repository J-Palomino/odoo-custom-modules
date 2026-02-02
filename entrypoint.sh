#!/bin/bash
set -e

# Fix permissions on Odoo data directory (Railway volume)
# This runs as root before switching to odoo user
if [ -d /var/lib/odoo ]; then
    echo "Fixing permissions on /var/lib/odoo..."
    chown -R odoo:odoo /var/lib/odoo
    chmod -R 755 /var/lib/odoo
fi

# If installing modules, do it now as odoo user
if [ -n "$ODOO_INSTALL_MODULES" ]; then
    echo "Installing modules: $ODOO_INSTALL_MODULES"
    su - odoo -c "odoo -d ${ODOO_DATABASE:-odoo} -i $ODOO_INSTALL_MODULES --stop-after-init" || {
        echo "Module installation failed, but continuing startup..."
    }
fi

# If updating modules, do it now as odoo user
if [ -n "$ODOO_UPDATE_MODULES" ]; then
    echo "Updating modules: $ODOO_UPDATE_MODULES"
    su - odoo -c "odoo -d ${ODOO_DATABASE:-odoo} -u $ODOO_UPDATE_MODULES --stop-after-init" || {
        echo "Module update failed, but continuing startup..."
    }
fi

# Execute the original entrypoint or command as odoo user
exec gosu odoo "$@"
