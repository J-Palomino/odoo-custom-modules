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
    purchase_price_precision mint_automations mint_maintenance_form mint_command_center mint_embed \
    mint_push mint_banner mint_customer_api mint_dutchie_sync mint_mail_whitelist mint_inventory_ops \
    daisydo_theme daisydo_livechat daisydo_agents daisydo_multicompany daisydo_webhook \
    mint_oauth_only base_accounting_kit base_account_budget sign_oca \
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
# These modules cause registry failures during update (view validation errors,
# incompatible versions, missing dependencies)
echo "=== Scanning for broken modules to remove ==="
for mod in slack_sync sign_oca; do
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
    # Also check standard addons (writable runtime paths)
    if [ -d "/usr/lib/python3/dist-packages/addons/$mod" ]; then
        echo "=== Removing broken module $mod at /usr/lib/python3/dist-packages/addons/$mod ==="
        rm -rf "/usr/lib/python3/dist-packages/addons/$mod"
    fi
done
# List remaining contents of persistent addons for debugging
echo "=== Persistent volume addons contents ==="
ls -la /var/lib/odoo/addons/ 2>/dev/null || echo "No addons dir"
ls -la /var/lib/odoo/addons/19.0/ 2>/dev/null || echo "No 19.0 dir"

# Clean stale model references from uninstalled modules (sign_oca, etc.)
echo "=== Cleaning stale model references ==="
if [ -n "$HOST" ]; then
    python3 << 'PYCLEAN' 2>&1
import os, sys
try:
    import psycopg2
except ImportError:
    print("psycopg2 not available, skipping stale model cleanup")
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

    # Remove stale ir.model entries for uninstalled modules
    for model in ('sign.oca.field', 'sign.oca.role', 'sign.oca.request',
                  'sign.oca.item', 'sign.oca.template', 'sign.oca.type'):
        cur.execute("DELETE FROM ir_model WHERE model = %s", (model,))
        if cur.rowcount:
            print(f"Removed stale ir_model: {model} ({cur.rowcount} rows)")

    # Clean orphaned ir.attachment records pointing to missing filestore files
    cur.execute("""
        DELETE FROM ir_attachment
        WHERE store_fname IS NOT NULL
          AND store_fname != ''
          AND id NOT IN (SELECT res_id FROM ir_model_data WHERE model = 'ir.attachment')
          AND create_date < NOW() - INTERVAL '30 days'
          AND res_model IS NULL
    """)
    if cur.rowcount:
        print(f"Cleaned {cur.rowcount} orphaned attachments")

    # Pre-create inventory_status column for mint_inventory_ops (avoids serialization conflicts)
    cur.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'product_template' AND column_name = 'inventory_status'
    """)
    if not cur.fetchone():
        cur.execute("ALTER TABLE product_template ADD COLUMN inventory_status VARCHAR DEFAULT 'active'")
        cur.execute("ALTER TABLE product_template ADD COLUMN discontinue_reason TEXT")
        cur.execute("ALTER TABLE product_template ADD COLUMN discontinue_date DATE")
        cur.execute("ALTER TABLE product_template ADD COLUMN last_adjustment_id INTEGER")
        print("=== Pre-created inventory_status columns on product_template ===")

    # Enable stock tracking on all goods (Odoo 19: is_storable=True for stock.quant)
    cur.execute("""
        UPDATE product_template
        SET is_storable = true
        WHERE type = 'consu' AND (is_storable = false OR is_storable IS NULL)
    """)
    if cur.rowcount:
        print(f"=== Enabled stock tracking on {cur.rowcount} product templates ===")
    else:
        print("=== All product templates already storable ===")

    cur.close()
    conn.close()
except Exception as e:
    print(f"WARNING: stale model cleanup failed: {e}")
PYCLEAN
fi

# Fix mail_message and mail_mail missing primary keys (pre-existing DB issue)
# Uses Python/psycopg2 since psql may not be installed in the Docker image
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
            print(f"=== {table} table does not exist — skipping ===")
            continue

        cur.execute("""
            SELECT 1 FROM pg_constraint
            WHERE conrelid = %s::regclass AND contype = 'p'
        """, (table,))
        if cur.fetchone():
            print(f"=== {table} PK exists — OK ===")
        else:
            print(f"=== {table} PK missing — fixing ===")
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

    # Fix mail_mail_res_partner_rel FK constraint — make it ON DELETE CASCADE
    # This prevents module upgrades from failing with:
    # "constraint: mail_mail_res_partner_rel_mail_mail_id_fkey"
    cur.execute("""
        SELECT 1 FROM information_schema.tables
        WHERE table_name = 'mail_mail_res_partner_rel' AND table_schema = 'public'
    """)
    if cur.fetchone():
        # Clean orphaned rel rows (referencing deleted mail_mail records)
        cur.execute("""
            DELETE FROM mail_mail_res_partner_rel
            WHERE mail_mail_id NOT IN (SELECT id FROM mail_mail)
        """)
        if cur.rowcount > 0:
            print(f"=== Cleaned {cur.rowcount} orphaned mail_mail_res_partner_rel rows ===")

        # Recreate FK with CASCADE
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

    cur.close()
    conn.close()
except Exception as e:
    print(f"=== WARNING: PK fix failed: {e} ===")
PYFIX
fi

# Set admin password via Odoo shell (proper ORM method, handles auth module overrides)
ADMIN_PW="${ODOO_ADMIN_PASSWORD:-$ADMIN_PASSWORD}"
echo "=== PW DEBUG: ODOO_ADMIN_PASSWORD=$(test -n \"$ODOO_ADMIN_PASSWORD\" && echo YES || echo NO) ADMIN_PASSWORD=$(test -n \"$ADMIN_PASSWORD\" && echo YES || echo NO) ADMIN_PW=$(test -n \"$ADMIN_PW\" && echo YES || echo NO) ===" >&2
if [ -n "$ADMIN_PW" ]; then
    export ODOO_ADMIN_PASSWORD="$ADMIN_PW"
    echo "=== Setting admin password via Odoo shell ===" >&2
    timeout 180 python3 << 'PYEOF' 2>&1 || echo "Warning: Odoo shell password set failed" >&2
import sys, os, inspect
sys.path.insert(0, '/usr/lib/python3/dist-packages')
os.environ.setdefault('ODOO_RC', '/var/lib/odoo/odoo.conf')

import odoo
from odoo.tools import config
config.parse_config(['-c', '/var/lib/odoo/odoo.conf'])

# db_name may be a list in Odoo 19 config
db_name = config.get('db_name', 'odoo')
if isinstance(db_name, (list, tuple)):
    db_name = db_name[0] if db_name else 'odoo'
print(f'Loading registry for {db_name}...')

from odoo.modules.registry import Registry
registry = Registry(db_name)

with registry.cursor() as cr:
    from odoo.api import Environment, SUPERUSER_ID
    env = Environment(cr, SUPERUSER_ID, {})

    # Debug: print _check_credentials source and MRO
    user_model = env['res.users']
    cls = type(user_model)
    print(f'User model class: {cls}')
    print(f'MRO (auth-related):')
    for c in cls.__mro__:
        n = c.__name__
        if any(k in n.lower() for k in ['user', 'passkey', 'totp', 'auth', 'password']):
            print(f'  {c.__module__}.{n}')
            if hasattr(c, '_check_credentials') and '_check_credentials' in c.__dict__:
                src = inspect.getsource(c._check_credentials)
                print(f'    _check_credentials ({len(src)} chars):')
                print('    ' + src[:600].replace('\n', '\n    '))
                print()

    # Set password via ORM write (triggers _inverse_password)
    user = env['res.users'].browse(2)
    print(f'\nUser: {user.login}, active: {user.active}, company_id: {user.company_id.id}')

    pw = os.environ.get('ODOO_ADMIN_PASSWORD', '')
    if pw:
        user.write({'password': pw})
        cr.commit()
        print(f'Password set via ORM for uid=2')

        # Verify hash in DB
        cr.execute('SELECT SUBSTRING(password, 1, 60) FROM res_users WHERE id = 2')
        print(f'Hash prefix: {cr.fetchone()[0]}')
    else:
        print('ODOO_ADMIN_PASSWORD not set in env')
PYEOF
    echo ""
fi

# Copy new modules to persistent volume so Odoo's data_dir scan finds them
echo "=== Syncing custom modules to persistent addons ==="
mkdir -p /var/lib/odoo/addons/19.0
for mod in mint_maintenance_form mint_automations mint_oauth_only mint_inventory_ops; do
    if [ -d "/opt/extra-addons/$mod" ]; then
        echo "Copying $mod to /var/lib/odoo/addons/19.0/$mod"
        rm -rf "/var/lib/odoo/addons/19.0/$mod"
        cp -r "/opt/extra-addons/$mod" "/var/lib/odoo/addons/19.0/$mod"
        chown -R odoo:odoo "/var/lib/odoo/addons/19.0/$mod"
    fi
done

# Debug: list what's in addons paths
echo "=== /opt/extra-addons contents ==="
ls /opt/extra-addons/ 2>/dev/null || echo "EMPTY or MISSING"
echo "=== CRITICAL: mint_maintenance_form check ==="
ls -la /opt/extra-addons/mint_maintenance_form/ 2>/dev/null || echo "DIR MISSING: /opt/extra-addons/mint_maintenance_form/"
test -f /opt/extra-addons/mint_maintenance_form/__manifest__.py && echo "MANIFEST OK at /opt/extra-addons/mint_maintenance_form/__manifest__.py" || echo "MANIFEST MISSING at /opt/extra-addons/mint_maintenance_form/__manifest__.py"
echo "=== /var/lib/odoo/addons/19.0 contents ==="
ls /var/lib/odoo/addons/19.0/ 2>/dev/null || echo "EMPTY or MISSING"
ls -la /var/lib/odoo/addons/19.0/mint_maintenance_form/ 2>/dev/null || echo "DIR MISSING: /var/lib/odoo/addons/19.0/mint_maintenance_form/"
test -f /var/lib/odoo/addons/19.0/mint_maintenance_form/__manifest__.py && echo "MANIFEST OK at /var/lib/odoo/addons/19.0/" || echo "MANIFEST MISSING at /var/lib/odoo/addons/19.0/"
echo "=== Check dependency modules on disk ==="
ls -d /usr/lib/python3/dist-packages/odoo/addons/maintenance/ 2>/dev/null && echo "MAINTENANCE: OK" || echo "MAINTENANCE: MISSING"
ls -d /usr/lib/python3/dist-packages/odoo/addons/website/ 2>/dev/null && echo "WEBSITE: OK" || echo "WEBSITE: MISSING"

# ── Start nginx reverse proxy for websocket routing ──────────────────
NGINX_PORT="${PORT:-8080}"
echo "=== Configuring nginx on port $NGINX_PORT ==="
sed "s/__NGINX_PORT__/$NGINX_PORT/g" /etc/nginx/nginx.conf.template > /etc/nginx/nginx.conf
nginx -t 2>&1 && echo "nginx config OK" || echo "WARNING: nginx config test failed"
nginx
echo "=== nginx started (pid $(cat /run/nginx.pid 2>/dev/null || echo 'unknown')) ==="

# Ensure gevent_port is set in config for websocket support
if [ -f "$CONFIG_FILE" ]; then
    if grep -q "gevent_port" "$CONFIG_FILE"; then
        sed -i 's/gevent_port\s*=\s*.*/gevent_port = 8072/g' "$CONFIG_FILE"
    else
        echo "gevent_port = 8072" >> "$CONFIG_FILE"
    fi
    echo "gevent_port = 8072 set in config"
fi

# Apply ODOO_WORKERS env var override (default: 6)
WORKERS="${ODOO_WORKERS:-6}"
if [ -f "$CONFIG_FILE" ]; then
    if grep -q "workers" "$CONFIG_FILE"; then
        sed -i "s/workers\s*=\s*[0-9]*/workers = $WORKERS/g" "$CONFIG_FILE"
    else
        echo "workers = $WORKERS" >> "$CONFIG_FILE"
    fi
    echo "workers = $WORKERS set in config"
fi

# Build extra args from environment variables
EXTRA_ARGS=""
if [ -n "$ODOO_UPDATE_MODULES" ] && [ "$ODOO_UPDATE_MODULES" != "none" ]; then
    echo "=== Updating modules: $ODOO_UPDATE_MODULES ==="
    EXTRA_ARGS="$EXTRA_ARGS --update $ODOO_UPDATE_MODULES"
fi
if [ -n "$ODOO_INIT_MODULES" ] && [ "$ODOO_INIT_MODULES" != "none" ]; then
    echo "=== Installing modules: $ODOO_INIT_MODULES ==="
    EXTRA_ARGS="$EXTRA_ARGS --init $ODOO_INIT_MODULES"
fi

# Execute the original entrypoint
exec /entrypoint.sh "$@" $EXTRA_ARGS
