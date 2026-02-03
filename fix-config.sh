#!/bin/bash
# Fix the odoo.conf on the persistent volume

CONFIG_FILE="/var/lib/odoo/odoo.conf"

if [ -f "$CONFIG_FILE" ]; then
    echo "Fixing db_port in $CONFIG_FILE..."
    # Replace db_port = 8069 with db_port = 5432
    sed -i 's/db_port = 8069/db_port = 5432/g' "$CONFIG_FILE"
    sed -i 's/db_port=8069/db_port=5432/g' "$CONFIG_FILE"

    # Also fix any HOST issues if present
    if [ -n "$HOST" ]; then
        sed -i "s/db_host = .*/db_host = $HOST/g" "$CONFIG_FILE"
    fi

    echo "Config file fixed."
    cat "$CONFIG_FILE" | grep -E "^(db_port|db_host)"
else
    echo "Config file not found at $CONFIG_FILE"
fi

# Execute the original entrypoint
exec /entrypoint.sh "$@"
