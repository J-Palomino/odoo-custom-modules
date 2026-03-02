#!/bin/bash
# Fix the odoo.conf on the persistent volume

CONFIG_FILE="/var/lib/odoo/odoo.conf"
ACTIVE_CONFIG="/etc/odoo/odoo.conf"

# Map ODOO_DB_* env vars to what the official Odoo entrypoint expects
[ -n "$ODOO_DB_HOST" ] && [ -z "$HOST" ] && export HOST="$ODOO_DB_HOST"
[ -n "$ODOO_DB_PORT" ] && [ -z "$PORT" ] && export PORT="$ODOO_DB_PORT"
[ -n "$ODOO_DB_USER" ] && [ -z "$USER" ] && export USER="$ODOO_DB_USER"
[ -n "$ODOO_DB_PASSWORD" ] && [ -z "$PASSWORD" ] && export PASSWORD="$ODOO_DB_PASSWORD"

echo "=== Debugging config fix ==="

# If config missing on persistent volume, seed from Docker image backup
if [ ! -f "$CONFIG_FILE" ] && [ -f "$ACTIVE_CONFIG" ]; then
    echo "Config file not found, seeding from $ACTIVE_CONFIG"
    cp "$ACTIVE_CONFIG" "$CONFIG_FILE"
    chown odoo:odoo "$CONFIG_FILE" 2>/dev/null || true
fi

# Helper: apply config fixes to a given file
fix_config() {
    local cfg="$1"
    if [ ! -f "$cfg" ]; then
        echo "Config file not found at $cfg"
        return
    fi

    echo "Fixing $cfg ..."

    # ── Workers ─────────────────────────────────────────────────────
    # Nginx handles routing websocket traffic to port 8072 (gevent),
    # so we can safely use multi-worker mode on Railway.
    WORKERS="${ODOO_WORKERS:-4}"
    if grep -q "workers" "$cfg"; then
        sed -i "s/workers\s*=\s*[0-9]*/workers = $WORKERS/g" "$cfg"
    else
        echo "workers = $WORKERS" >> "$cfg"
    fi
    echo "Set workers = $WORKERS"

    # Replace any db_port setting with 5432
    sed -i 's/db_port\s*=\s*[0-9]*/db_port = 5432/g' "$cfg"

    # If db_port doesn't exist, add it
    if ! grep -q "db_port" "$cfg"; then
        echo "db_port = 5432" >> "$cfg"
        echo "Added db_port = 5432 to config"
    fi

    # Fix db_host if HOST env var is present
    if [ -n "$HOST" ]; then
        if grep -q "db_host" "$cfg"; then
            sed -i "s/db_host\s*=\s*.*/db_host = $HOST/g" "$cfg"
        else
            echo "db_host = $HOST" >> "$cfg"
        fi
        echo "Set db_host = $HOST"
    fi

    # Fix db_name if ODOO_DB_NAME env var is present
    if [ -n "$ODOO_DB_NAME" ]; then
        if grep -q "db_name" "$cfg"; then
            sed -i "s/db_name\s*=\s*.*/db_name = $ODOO_DB_NAME/g" "$cfg"
        else
            echo "db_name = $ODOO_DB_NAME" >> "$cfg"
        fi
    fi

    # Fix addons_path to ensure /opt/extra-addons is included
    EXPECTED_ADDONS="/opt/extra-addons,/usr/lib/python3/dist-packages/odoo/addons"
    if grep -q "addons_path" "$cfg"; then
        sed -i "s|addons_path\s*=\s*.*|addons_path = $EXPECTED_ADDONS|g" "$cfg"
        echo "Fixed addons_path to: $EXPECTED_ADDONS"
    else
        echo "addons_path = $EXPECTED_ADDONS" >> "$cfg"
        echo "Added addons_path to config"
    fi
}

# Fix both the persistent-volume config and the active config that Odoo reads
fix_config "$CONFIG_FILE"
fix_config "$ACTIVE_CONFIG"

echo ""
echo "Active config ($ACTIVE_CONFIG):"
grep -iE "db_|port|workers|addons_path" "$ACTIVE_CONFIG" || echo "(no matching lines)"
echo ""
echo "Persistent config ($CONFIG_FILE):"
grep -iE "db_|port|workers|addons_path" "$CONFIG_FILE" || echo "(no matching lines)"

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
    mint_maintenance_form mint_push mint_command_center \
    daisydo_theme daisydo_livechat daisydo_agents daisydo_multicompany daisydo_webhook \
    base_accounting_kit base_account_budget sign_oca \
    dms dms_field hr_dms_field \
    fs_storage fs_attachment fs_attachment_s3 server_environment \
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

# Fix mail table primary keys and FK constraints (pre-existing DB issue)
echo "=== Checking mail table primary keys ==="
if [ -n "$HOST" ]; then
    python3 << 'PYFIX' 2>&1
import os, sys
try:
    import psycopg2
except ImportError:
    print("psycopg2 not available, skipping PK fix")
    sys.exit(0)

host = os.environ.get("HOST", "localhost")
port = os.environ.get("PORT", "5432")
user = os.environ.get("USER", "odoo")
password = os.environ.get("PASSWORD", os.environ.get("ODOO_DB_PASSWORD", ""))
dbname = os.environ.get("ODOO_DB_NAME", "odoo")

try:
    conn = psycopg2.connect(host=host, port=port, user=user, password=password, dbname=dbname)
    conn.autocommit = True
    cur = conn.cursor()

    for table in ("mail_message", "mail_mail"):
        cur.execute("""
            SELECT 1 FROM information_schema.tables
            WHERE table_name = %s AND table_schema = 'public'
        """, (table,))
        if not cur.fetchone():
            print(f"=== {table} table does not exist -- skipping ===")
            continue

        cur.execute("""
            SELECT 1 FROM pg_constraint
            WHERE conrelid = %s::regclass AND contype = 'p'
        """, (table,))
        if cur.fetchone():
            print(f"=== {table} PK exists -- OK ===")
        else:
            print(f"=== {table} PK missing -- fixing ===")
            cur.execute(f"""
                DELETE FROM {table}
                WHERE ctid IN (
                    SELECT ctid FROM (
                        SELECT ctid, ROW_NUMBER() OVER (PARTITION BY id ORDER BY ctid) as rn
                        FROM {table}
                        WHERE id IN (SELECT id FROM {table} GROUP BY id HAVING COUNT(*) > 1)
                    ) sub WHERE rn > 1
                )
            """)
            print(f"Deleted {cur.rowcount} duplicate {table} rows")
            cur.execute(f"ALTER TABLE {table} ADD PRIMARY KEY (id)")
            print(f"=== {table} PK restored successfully ===")

    # Fix mail_mail_res_partner_rel FK constraint -- make it ON DELETE CASCADE
    cur.execute("""
        SELECT 1 FROM information_schema.tables
        WHERE table_name = 'mail_mail_res_partner_rel' AND table_schema = 'public'
    """)
    if cur.fetchone():
        cur.execute("""
            DELETE FROM mail_mail_res_partner_rel
            WHERE mail_mail_id NOT IN (SELECT id FROM mail_mail)
        """)
        if cur.rowcount > 0:
            print(f"=== Cleaned {cur.rowcount} orphaned mail_mail_res_partner_rel rows ===")

        cur.execute("""
            SELECT 1 FROM pg_constraint
            WHERE conname = 'mail_mail_res_partner_rel_mail_mail_id_fkey'
        """)
        if cur.fetchone():
            cur.execute("""
                ALTER TABLE mail_mail_res_partner_rel
                DROP CONSTRAINT mail_mail_res_partner_rel_mail_mail_id_fkey
            """)
            cur.execute("""
                ALTER TABLE mail_mail_res_partner_rel
                ADD CONSTRAINT mail_mail_res_partner_rel_mail_mail_id_fkey
                FOREIGN KEY (mail_mail_id) REFERENCES mail_mail(id) ON DELETE CASCADE
            """)
            print("=== mail_mail_res_partner_rel FK recreated with CASCADE ===")

    # Check if daisydo_agents needs upgrading (version mismatch)
    cur.execute("""
        SELECT state, latest_version FROM ir_module_module
        WHERE name = 'daisydo_agents'
    """)
    row = cur.fetchone()
    if row:
        state, db_ver = row
        disk_ver = None
        try:
            import ast
            with open('/opt/extra-addons/daisydo_agents/__manifest__.py') as f:
                manifest = ast.literal_eval(f.read())
            disk_ver = manifest.get('version', '')
        except Exception:
            pass

        print(f"=== daisydo_agents: state={state}, db_ver={db_ver}, disk_ver={disk_ver} ===")
        if disk_ver and db_ver != disk_ver and state == 'installed':
            cur.execute("""
                UPDATE ir_module_module SET state = 'to upgrade'
                WHERE name = 'daisydo_agents'
            """)
            print(f"=== Set daisydo_agents to 'to upgrade' (db={db_ver} -> disk={disk_ver}) ===")
            # Write a flag file so fix-config.sh knows to add --update
            with open('/tmp/_need_upgrade_daisydo_agents', 'w') as f:
                f.write(disk_ver)

    cur.close()
    conn.close()
except Exception as e:
    print(f"=== WARNING: DB fix failed: {e} ===")
PYFIX
fi

# Build extra args from environment variables
EXTRA_ARGS=""

# Auto-detect module upgrades needed (from DB version check above)
if [ -f /tmp/_need_upgrade_daisydo_agents ]; then
    echo "=== Auto-upgrade detected: daisydo_agents needs update ==="
    EXTRA_ARGS="$EXTRA_ARGS --update daisydo_agents"
    rm -f /tmp/_need_upgrade_daisydo_agents
fi

if [ -n "$ODOO_UPDATE_MODULES" ] && [ "$ODOO_UPDATE_MODULES" != "none" ]; then
    echo "=== Updating modules: $ODOO_UPDATE_MODULES ==="
    EXTRA_ARGS="$EXTRA_ARGS --update $ODOO_UPDATE_MODULES"
fi
if [ -n "$ODOO_INIT_MODULES" ] && [ "$ODOO_INIT_MODULES" != "none" ]; then
    echo "=== Installing modules: $ODOO_INIT_MODULES ==="
    EXTRA_ARGS="$EXTRA_ARGS --init $ODOO_INIT_MODULES"
fi

# ── Fix volume permissions (Railway mounts as ubuntu) ─────────────
if [ -d /var/lib/odoo ]; then
    chown -R odoo:odoo /var/lib/odoo 2>/dev/null || true
fi

# ── Start nginx + Odoo (Railway mode) or just Odoo (local) ────────
if [ -n "${PORT:-}" ]; then
    # Railway mode: nginx proxies $PORT → 8069 (HTTP) + 8072 (websocket)
    mkdir -p /etc/nginx/conf.d
    envsubst '${PORT}' < /etc/nginx/templates/odoo.conf.template > /etc/nginx/conf.d/default.conf
    rm -f /etc/nginx/sites-enabled/default
    echo "Starting nginx on port $PORT..."
    nginx -g 'daemon off;' &
    NGINX_PID=$!

    # Build DB args (same as official entrypoint)
    DB_ARGS=""
    [ -n "$HOST" ] && DB_ARGS="$DB_ARGS --db_host=$HOST"
    [ -n "$USER" ] && DB_ARGS="$DB_ARGS --db_user=$USER"
    [ -n "$PASSWORD" ] && DB_ARGS="$DB_ARGS --db_password=$PASSWORD"

    echo "Starting Odoo (HTTP=8080, gevent=8072)..."
    su -s /bin/bash odoo -c "odoo \
        -c /etc/odoo/odoo.conf \
        --http-port=8080 \
        --gevent-port=8072 \
        $DB_ARGS \
        $EXTRA_ARGS" &
    ODOO_PID=$!

    # If either process dies, kill the other and exit
    wait -n "$ODOO_PID" "$NGINX_PID"
    echo "Process exited, shutting down..."
    kill "$ODOO_PID" "$NGINX_PID" 2>/dev/null || true
    wait
else
    # Local dev: run Odoo directly (both ports accessible)
    exec /entrypoint.sh "$@" $EXTRA_ARGS
fi
