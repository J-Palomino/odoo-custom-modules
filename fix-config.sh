#!/bin/bash
# Fix the odoo.conf on the persistent volume

CONFIG_FILE="/var/lib/odoo/odoo.conf"

# Map ODOO_DB_* env vars to what the official Odoo entrypoint expects
[ -n "$ODOO_DB_HOST" ] && [ -z "$HOST" ] && export HOST="$ODOO_DB_HOST"
[ -n "$ODOO_DB_PORT" ] && [ -z "$PORT" ] && export PORT="$ODOO_DB_PORT"
[ -n "$ODOO_DB_USER" ] && [ -z "$USER" ] && export USER="$ODOO_DB_USER"
[ -n "$ODOO_DB_PASSWORD" ] && [ -z "$PASSWORD" ] && export PASSWORD="$ODOO_DB_PASSWORD"

echo "=== Debugging config fix ==="

# If config missing on persistent volume, seed from Docker image backup
if [ ! -f "$CONFIG_FILE" ] && [ -f "/etc/odoo/odoo.conf" ]; then
    echo "Config file not found, seeding from /etc/odoo/odoo.conf"
    cp /etc/odoo/odoo.conf "$CONFIG_FILE"
    chown odoo:odoo "$CONFIG_FILE" 2>/dev/null || true
fi

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

    # Fix db_host if HOST env var is present
    if [ -n "$HOST" ]; then
        if grep -q "db_host" "$CONFIG_FILE"; then
            sed -i "s/db_host\s*=\s*.*/db_host = $HOST/g" "$CONFIG_FILE"
        else
            echo "db_host = $HOST" >> "$CONFIG_FILE"
        fi
        echo "Set db_host = $HOST"
    fi

    # Fix db_name if ODOO_DB_NAME env var is present
    if [ -n "$ODOO_DB_NAME" ]; then
        if grep -q "db_name" "$CONFIG_FILE"; then
            sed -i "s/db_name\s*=\s*.*/db_name = $ODOO_DB_NAME/g" "$CONFIG_FILE"
        else
            echo "db_name = $ODOO_DB_NAME" >> "$CONFIG_FILE"
        fi
    fi

    # Fix addons_path to ensure /opt/extra-addons is included
    EXPECTED_ADDONS="/opt/extra-addons,/usr/lib/python3/dist-packages/odoo/addons"
    if grep -q "addons_path" "$CONFIG_FILE"; then
        sed -i "s|addons_path\s*=\s*.*|addons_path = $EXPECTED_ADDONS|g" "$CONFIG_FILE"
        echo "Fixed addons_path to: $EXPECTED_ADDONS"
    else
        echo "addons_path = $EXPECTED_ADDONS" >> "$CONFIG_FILE"
        echo "Added addons_path to config"
    fi

    echo ""
    echo "Fixed config (db-related lines):"
    grep -i "db_\|port" "$CONFIG_FILE" || echo "No db_ or port lines found"
    echo "Addons path:"
    grep "addons_path" "$CONFIG_FILE"
else
    echo "Config file not found at $CONFIG_FILE"
fi

echo "=== End debugging ==="
echo ""

# Generate brand theme from environment variables
THEME_GENERATOR="/opt/extra-addons/mint_theme/generate-theme.sh"
if [ -f "$THEME_GENERATOR" ]; then
    echo "=== Generating brand theme ==="
    bash "$THEME_GENERATOR"
    echo ""
else
    echo "Theme generator not found at $THEME_GENERATOR"
fi

# Generate DaisyDo theme from same environment variables
DAISY_GENERATOR="/opt/extra-addons/daisydo_theme/generate-theme.sh"
if [ -f "$DAISY_GENERATOR" ]; then
    echo "=== Generating DaisyDo theme ==="
    bash "$DAISY_GENERATOR"
    echo ""
else
    echo "DaisyDo theme generator not found at $DAISY_GENERATOR"
fi

# Remove stale base_import_module copies from persistent volume
# (base_import_module installs to persistent volume paths which are
# scanned before /opt/extra-addons/ — we need the Docker version)
for mod in daisy_bot mint_theme mint_api_v2 avancir_inventory vault account_financial_risk \
    mint_maintenance_form \
    daisydo_theme daisydo_livechat daisydo_agents daisydo_multicompany daisydo_webhook \
    base_accounting_kit base_account_budget sign_oca \
    spreadsheet_oca spreadsheet_dashboard_oca \
    base_cancel_confirm base_substate base_technical_features date_range \
    bi_sql_editor report_qweb_element_page_visibility report_xlsx report_xlsx_helper report_xml sql_request_abstract \
    account_analytic_tag account_invoice_start_end_dates \
    account_financial_report account_tax_balance partner_statement \
    account_account_tag_code account_journal_restrict_mode account_move_name_sequence \
    account_move_post_date_user account_move_print account_usability \
    account_invoice_fixed_discount account_invoice_pricelist account_invoice_pricelist_sale \
    account_statement_base; do
    for d in /var/lib/odoo/addons/*/$mod /var/lib/odoo/addons/$mod; do
        if [ -d "$d" ]; then
            echo "=== Removing stale $mod at $d ==="
            rm -rf "$d"
        fi
    done
done

# Remove broken/non-installable modules from ALL addons paths
# slack_sync causes "inconsistent states" errors and blocks module finalization
echo "=== Scanning for broken modules to remove ==="
for mod in slack_sync avancir_inventory spreadsheet_oca spreadsheet_dashboard_oca sign_oca; do
    # Check /opt/extra-addons (Docker image baked-in modules)
    if [ -d "/opt/extra-addons/$mod" ]; then
        echo "=== Removing broken module $mod at /opt/extra-addons/$mod ==="
        rm -rf "/opt/extra-addons/$mod"
    fi
    # Search persistent volume addons paths
    find /var/lib/odoo/addons -name "$mod" -type d 2>/dev/null | while read d; do
        echo "=== Removing broken module $mod at $d ==="
        rm -rf "$d"
    done
    # Also check /usr/lib/python3/dist-packages/addons (writable runtime paths)
    if [ -d "/usr/lib/python3/dist-packages/addons/$mod" ]; then
        echo "=== Removing broken module $mod at /usr/lib/python3/dist-packages/addons/$mod ==="
        rm -rf "/usr/lib/python3/dist-packages/addons/$mod"
    fi
done
# List remaining contents of persistent addons for debugging
echo "=== Persistent volume addons contents ==="
ls -la /var/lib/odoo/addons/ 2>/dev/null || echo "No addons dir"
ls -la /var/lib/odoo/addons/19.0/ 2>/dev/null || echo "No 19.0 dir"

# Build extra args from environment variables
EXTRA_ARGS=""
if [ -n "$ODOO_UPDATE_MODULES" ]; then
    echo "=== Updating modules: $ODOO_UPDATE_MODULES ==="
    EXTRA_ARGS="$EXTRA_ARGS --update $ODOO_UPDATE_MODULES"
fi
if [ -n "$ODOO_INIT_MODULES" ]; then
    echo "=== Installing modules: $ODOO_INIT_MODULES ==="
    EXTRA_ARGS="$EXTRA_ARGS --init $ODOO_INIT_MODULES"
fi

# Execute the original entrypoint
exec /entrypoint.sh "$@" $EXTRA_ARGS
