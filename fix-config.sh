#!/bin/bash
# Fix the odoo.conf on the persistent volume

CONFIG_FILE="/var/lib/odoo/odoo.conf"

echo "=== Debugging config fix ==="

if [ -f "$CONFIG_FILE" ]; then
    echo "Original config (db-related lines):"
    grep -i "db_\|port" "$CONFIG_FILE" || echo "No db_ or port lines found"

    echo ""
    echo "Fixing db_port in $CONFIG_FILE..."

    # Replace any db_port setting with 5432
    sed -i 's/db_port\s*=\s*[0-9]*/db_port = 5432/g' "$CONFIG_FILE"

    # If db_port doesn't exist, add it
    if ! grep -q "db_port" "$CONFIG_FILE"; then
        echo "db_port = 5432" >> "$CONFIG_FILE"
        echo "Added db_port = 5432 to config"
    fi

    # Fix HOST if present
    if [ -n "$HOST" ]; then
        sed -i "s/db_host\s*=\s*.*/db_host = $HOST/g" "$CONFIG_FILE"
    fi

    echo ""
    echo "Fixed config (db-related lines):"
    grep -i "db_\|port" "$CONFIG_FILE" || echo "No db_ or port lines found"
else
    echo "Config file not found at $CONFIG_FILE"
fi

echo "=== End debugging ==="
echo ""

# Generate brand theme from environment variables
THEME_GENERATOR="/mnt/extra-addons/mint_theme/generate-theme.sh"
if [ -f "$THEME_GENERATOR" ]; then
    echo "=== Generating brand theme ==="
    bash "$THEME_GENERATOR"
    echo ""
else
    echo "Theme generator not found at $THEME_GENERATOR"
fi

# Remove stale base_import_module copies from persistent volume
# (base_import_module installs to persistent volume paths which are
# scanned before /mnt/extra-addons/ — we need the Docker version)
for d in /var/lib/odoo/addons/*/daisy_bot /var/lib/odoo/addons/daisy_bot; do
    if [ -d "$d" ]; then
        echo "=== Removing stale daisy_bot at $d ==="
        rm -rf "$d"
    fi
done

# Build extra args from environment variables
EXTRA_ARGS=""
if [ -n "$ODOO_UPDATE_MODULES" ]; then
    echo "=== Updating modules: $ODOO_UPDATE_MODULES ==="
    EXTRA_ARGS="$EXTRA_ARGS --update $ODOO_UPDATE_MODULES"
fi

# Execute the original entrypoint
exec /entrypoint.sh "$@" $EXTRA_ARGS
