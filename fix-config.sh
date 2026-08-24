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
    mint_posthog daisy_error_handler \
    daisydo_theme daisydo_livechat daisydo_agents daisydo_multicompany daisydo_webhook \
    mint_oauth_only mint_redis_session base_accounting_kit base_account_budget \
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
    # OCA Tier Validation / Knowledge / QMS bundle removed 2026-05-20 —
    # see Dockerfile comment + memory entry oca-port-bundle-v19-broken.
    for d in /var/lib/odoo/addons/*/$mod /var/lib/odoo/addons/$mod; do
        if [ -d "$d" ]; then
            echo "=== Removing stale $mod at $d ==="
            rm -rf "$d"
        fi
    done
done

# Why: nuke compiled .pyc caches under /opt/extra-addons so Python re-compiles
# from the freshly-COPY-ed .py sources every cold start. Without this, a stale
# .pyc with an outdated method signature can survive across deploys and the
# new .py source on disk is effectively ignored by the import system.
echo "=== Wiping .pyc / __pycache__ under /opt/extra-addons ==="
find /opt/extra-addons -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null
find /opt/extra-addons -name '*.pyc' -delete 2>/dev/null

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

# Create placeholder files for known missing filestore entries (prevents recurring errors)
echo "=== Creating filestore placeholders for orphaned references ==="
for f in 80/80eb20a26a4c1b41d6312a93a948c86f49a985ed \
         96/9667dcb4a161fe69b41a2476f4a78c309ca8a92d \
         18/187ac5dc623b63f9d57c6c6b18945da9f84a787d \
         29/292223f3ab3d90064af39468aba40d07fc907814; do
    dir="/var/lib/odoo/filestore/odoo/$(dirname "$f")"
    path="/var/lib/odoo/filestore/odoo/$f"
    if [ ! -f "$path" ]; then
        mkdir -p "$dir" 2>/dev/null || true
        printf '\x89PNG\r\n\x1a\n' > "$path" 2>/dev/null || true
        chown odoo:odoo "$path" 2>/dev/null || true
        echo "Created placeholder: $path"
    fi
done

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

    # Remove ghost modules that don't exist in the codebase
    for mod in ('daisydo_mcp',):
        cur.execute("DELETE FROM ir_model_data WHERE module = %s", (mod,))
        deleted_data = cur.rowcount
        cur.execute("DELETE FROM ir_module_module_dependency WHERE module_id IN (SELECT id FROM ir_module_module WHERE name = %s)", (mod,))
        cur.execute("DELETE FROM ir_module_module WHERE name = %s", (mod,))
        if cur.rowcount:
            print(f"Removed ghost module '{mod}' from DB ({deleted_data} data records)")

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

    # Configure fs_storage backend for iDrive e2 S3 (if table exists)
    # Uses env vars for credentials — never hardcoded
    cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'fs_storage'")
    if cur.fetchone():
        s3_endpoint = os.environ.get("AWS_S3_ENDPOINT", "https://s3.us-southwest-1.idrivee2.com")
        s3_bucket = os.environ.get("AWS_S3_BUCKET", "letsgomint-prod")
        s3_region = os.environ.get("AWS_S3_REGION", "us-southwest-1")
        s3_key = os.environ.get("AWS_ACCESS_KEY_ID", "")
        s3_secret = os.environ.get("AWS_SECRET_ACCESS_KEY", "")

        if s3_key and s3_secret:
            import json
            options_json = json.dumps({
                "endpoint_url": s3_endpoint,
                "key": s3_key,
                "secret": s3_secret,
            })

            # Upsert the idrive_e2 storage backend
            cur.execute("SELECT id FROM fs_storage WHERE code = 'idrive_e2'")
            row = cur.fetchone()
            if row:
                cur.execute("""
                    UPDATE fs_storage SET
                        options = %s,
                        directory_path = %s
                    WHERE code = 'idrive_e2'
                """, (options_json, s3_bucket))
                print(f"=== Updated fs_storage 'idrive_e2' → bucket={s3_bucket} ===")
            else:
                cur.execute("""
                    INSERT INTO fs_storage (name, code, protocol, options, directory_path, create_uid, create_date, write_uid, write_date)
                    VALUES ('iDrive e2 S3', 'idrive_e2', 's3', %s, %s, 1, NOW(), 1, NOW())
                """, (options_json, s3_bucket))
                print(f"=== Created fs_storage 'idrive_e2' → bucket={s3_bucket} ===")
        else:
            print("=== S3 credentials not set — skipping fs_storage config ===")

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

    # Fix mail_message_res_partner_rel FK constraint — same issue for mail_message
    # Prevents: "constraint: mail_message_res_partner_rel_mail_message_id_fkey"
    cur.execute("""
        SELECT 1 FROM information_schema.tables
        WHERE table_name = 'mail_message_res_partner_rel' AND table_schema = 'public'
    """)
    if cur.fetchone():
        cur.execute("""
            DELETE FROM mail_message_res_partner_rel
            WHERE mail_message_id NOT IN (SELECT id FROM mail_message)
        """)
        if cur.rowcount > 0:
            print(f"=== Cleaned {cur.rowcount} orphaned mail_message_res_partner_rel rows ===")

        cur.execute("""
            SELECT 1 FROM pg_constraint
            WHERE conname = 'mail_message_res_partner_rel_mail_message_id_fkey'
        """)
        if cur.fetchone():
            cur.execute("""
                ALTER TABLE mail_message_res_partner_rel
                DROP CONSTRAINT mail_message_res_partner_rel_mail_message_id_fkey
            """)
            cur.execute("""
                ALTER TABLE mail_message_res_partner_rel
                ADD CONSTRAINT mail_message_res_partner_rel_mail_message_id_fkey
                FOREIGN KEY (mail_message_id) REFERENCES mail_message(id) ON DELETE CASCADE
            """)
            print("=== mail_message_res_partner_rel FK recreated with CASCADE ===")

    # Pre-create missing res_config_settings columns for daisydo_agents
    for col in ('daisy_global_api_key', 'daisy_template_chatflow_id', 'go2rtc_api_url', 'go2rtc_public_url'):
        cur.execute("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'res_config_settings' AND column_name = %s
        """, (col,))
        if not cur.fetchone():
            cur.execute(f"ALTER TABLE res_config_settings ADD COLUMN {col} VARCHAR")
            print(f"=== Pre-created column res_config_settings.{col} ===")

    cur.close()
    conn.close()
except Exception as e:
    print(f"=== WARNING: PK fix failed: {e} ===")
PYFIX
fi

# Set admin password via Odoo shell (proper ORM method, handles auth module overrides)
ADMIN_PW="${ODOO_ADMIN_PASSWORD:-$ADMIN_PASSWORD}"
if [ -n "$ADMIN_PW" ]; then
    export ODOO_ADMIN_PASSWORD="$ADMIN_PW"
    echo "=== Setting admin password via Odoo shell ===" >&2
    timeout 180 python3 << 'PYEOF' 2>&1 || echo "Warning: Odoo shell password set failed" >&2
import sys, os
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
for mod in mint_maintenance_form mint_automations mint_oauth_only mint_inventory_ops purchase_price_precision; do
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
echo "=== /var/lib/odoo/addons/19.0 contents ==="
ls /var/lib/odoo/addons/19.0/ 2>/dev/null || echo "EMPTY or MISSING"
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

# Why: the persistent volume's odoo.conf is only seeded from /etc/odoo/odoo.conf
# on first boot; subsequent deploys never re-copy. Volumes that were seeded before
# `proxy_mode = True` was added to the bundled config stay stale forever, which
# silently breaks OAuth SSO (request.scheme falls back to 'http' inside the
# container, Google rejects with redirect_uri_mismatch). Same self-healing
# pattern as workers/gevent_port above.
PROXY_MODE_VALUE="${PROXY_MODE:-True}"
if [ -f "$CONFIG_FILE" ]; then
    if grep -q "^proxy_mode" "$CONFIG_FILE"; then
        sed -i "s/^proxy_mode\s*=\s*.*/proxy_mode = $PROXY_MODE_VALUE/g" "$CONFIG_FILE"
    else
        echo "proxy_mode = $PROXY_MODE_VALUE" >> "$CONFIG_FILE"
    fi
    echo "proxy_mode = $PROXY_MODE_VALUE set in config"
fi

# Ensure server_wide_modules includes mint_redis_session (Redis sessions)
if [ -f "$CONFIG_FILE" ]; then
    if grep -q "server_wide_modules" "$CONFIG_FILE"; then
        if ! grep -q "mint_redis_session" "$CONFIG_FILE"; then
            sed -i "s/server_wide_modules\s*=\s*.*/& ,mint_redis_session/" "$CONFIG_FILE"
            echo "Added mint_redis_session to server_wide_modules"
        fi
    else
        echo "server_wide_modules = base,web,mint_redis_session" >> "$CONFIG_FILE"
        echo "Set server_wide_modules = base,web,mint_redis_session"
    fi
fi

# ── Auto-detect module version drift ─────────────────────────────────
# Previously this script only ran `--update $ODOO_UPDATE_MODULES`, a
# hand-maintained env var. Any module missing from that list loaded its new
# Python but SILENTLY skipped its migrations, views and data files, so
# ir_module_module.latest_version (DB-recorded) fell behind the on-disk
# manifest version indefinitely and nobody noticed. The list also gets
# trimmed periodically and has been observed empty, so drift accumulated
# across many modules at once.
#
# Recompute the drift set on every boot instead, so latest_version is true
# by construction rather than by remembering to edit a variable.
# Set ODOO_AUTO_UPDATE_DRIFT=0 to disable (falls back to the old behaviour).
DRIFT_MODULES=""
if [ -n "$HOST" ] && [ "$ODOO_AUTO_UPDATE_DRIFT" != "0" ]; then
    echo "=== Detecting module version drift ===" >&2
    DRIFT_MODULES=$(python3 << 'PYDRIFT'
import os, sys, ast

try:
    import psycopg2
except ImportError:
    sys.exit(0)

SERIES = "19.0"
ADDONS = ("/opt/extra-addons", "/var/lib/odoo/addons/19.0")


def normalise(raw):
    """Mirror Odoo's own manifest-version normalisation."""
    v = str(raw or "").strip()
    if not v:
        return ""
    return v if v.startswith(SERIES + ".") else "%s.%s" % (SERIES, v)


def on_disk_version(name):
    for base in ADDONS:
        path = os.path.join(base, name, "__manifest__.py")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                manifest = ast.literal_eval(fh.read())
        except Exception as exc:
            print("  ! %s: unreadable manifest (%s)" % (name, exc), file=sys.stderr)
            return ""
        if isinstance(manifest, dict):
            return normalise(manifest.get("version", ""))
    return ""


def _clean(v):
    v = (v or "").strip()
    return "" if v.lower() in ("", "false", "none") else v


def db_params():
    """Env first, then the odoo.conf the server itself is about to use.

    Staging sets HOST but not PASSWORD, so an env-only lookup fails with
    "fe_sendauth: no password supplied" and the drift scan silently no-ops.
    """
    # NB: bare PORT and USER are NOT database settings on Railway. PORT is the
    # HTTP listen port (8080) — the header only maps ODOO_DB_PORT onto it when
    # PORT is unset, which on Railway it never is — and USER is the unix user.
    # Using them yields "port 8080 ... Connection refused". Prefer the explicit
    # ODOO_DB_* vars, then odoo.conf, and never fall back to bare PORT.
    p = {
        "host": _clean(os.environ.get("ODOO_DB_HOST")) or _clean(os.environ.get("HOST")),
        "port": _clean(os.environ.get("ODOO_DB_PORT")),
        "user": _clean(os.environ.get("ODOO_DB_USER")),
        "password": _clean(os.environ.get("ODOO_DB_PASSWORD")) or _clean(os.environ.get("PASSWORD")),
        "dbname": _clean(os.environ.get("ODOO_DB_NAME")),
    }
    cfg = "/var/lib/odoo/odoo.conf"
    if os.path.isfile(cfg):
        import configparser
        cp = configparser.ConfigParser()
        try:
            cp.read(cfg)
            sec = "options" if cp.has_section("options") else (cp.sections()[0] if cp.sections() else None)
            if sec:
                for key, opt in (("host", "db_host"), ("port", "db_port"), ("user", "db_user"),
                                 ("password", "db_password"), ("dbname", "db_name")):
                    if not p[key]:
                        p[key] = _clean(cp.get(sec, opt, fallback=""))
        except Exception as exc:
            print("  ! could not read %s (%s)" % (cfg, exc), file=sys.stderr)
    p["host"] = p["host"] or "localhost"
    p["port"] = p["port"] or "5432"
    p["user"] = p["user"] or _clean(os.environ.get("USER")) or "odoo"
    p["dbname"] = p["dbname"] or "odoo"
    print("  connecting to %s@%s:%s/%s" % (p["user"], p["host"], p["port"], p["dbname"]), file=sys.stderr)
    return p


try:
    conn = psycopg2.connect(**db_params())
    cur = conn.cursor()
    cur.execute("SELECT name, latest_version FROM ir_module_module WHERE state = 'installed'")
    rows = cur.fetchall()
    cur.close()
    conn.close()
except Exception as exc:
    # Emit a sentinel so the shell can tell "detection broke" apart from
    # "nothing drifted" — those looked identical and hid a real failure.
    print("  ! drift detection FAILED: %s" % exc, file=sys.stderr)
    sys.stdout.write("__DRIFT_DETECTION_FAILED__")
    sys.exit(0)

def version_tuple(v):
    out = []
    for part in str(v or "").split("."):
        out.append(int(part) if part.isdigit() else 0)
    return tuple(out)


EXCLUDE = {m.strip() for m in os.environ.get("ODOO_DRIFT_EXCLUDE", "").split(",") if m.strip()}

drifted, behind, skipped = [], [], 0
for name, recorded in rows:
    if name in EXCLUDE:
        skipped += 1
        continue
    disk = on_disk_version(name)
    if not disk:
        continue  # not one of ours / no manifest on the addons path
    recorded = recorded or ""
    if disk == recorded:
        continue
    if version_tuple(disk) > version_tuple(recorded):
        # Genuine pending upgrade: the deployed manifest is ahead of the DB.
        drifted.append(name)
        print("  drift: %s disk=%s db=%s" % (name, disk, recorded), file=sys.stderr)
    else:
        # DB ahead of disk. Re-running -u cannot fix this and may downgrade,
        # so only report it. Seen on the OCA bundle whose manifests read "1.0"
        # while the DB carries a real version — those modules are unloadable
        # anyway ("dependencies or manifest may be missing").
        behind.append(name)

if behind:
    print("  NOTE: %d module(s) have a DB version AHEAD of the deployed manifest "
          "(not upgrading): %s" % (len(behind), ",".join(sorted(behind))), file=sys.stderr)
if skipped:
    print("  %d module(s) skipped via ODOO_DRIFT_EXCLUDE" % skipped, file=sys.stderr)
print("  %d installed module(s) scanned, %d to upgrade" % (len(rows), len(drifted)), file=sys.stderr)
# stdout is captured by the shell — emit ONLY the comma list
sys.stdout.write(",".join(sorted(drifted)))
PYDRIFT
)
    if [ "$DRIFT_MODULES" = "__DRIFT_DETECTION_FAILED__" ]; then
        echo "=== WARNING: drift detection FAILED — falling back to ODOO_UPDATE_MODULES only ===" >&2
        DRIFT_MODULES=""
    elif [ -n "$DRIFT_MODULES" ]; then
        echo "=== Version drift detected, will upgrade: $DRIFT_MODULES ===" >&2
    else
        echo "=== No version drift detected ===" >&2
    fi
fi

# Build extra args from environment variables
EXTRA_ARGS=""
UPDATE_LIST="$ODOO_UPDATE_MODULES"
if [ "$UPDATE_LIST" = "none" ]; then
    UPDATE_LIST=""
fi
if [ -n "$DRIFT_MODULES" ]; then
    if [ -n "$UPDATE_LIST" ]; then
        UPDATE_LIST="$UPDATE_LIST,$DRIFT_MODULES"
    else
        UPDATE_LIST="$DRIFT_MODULES"
    fi
fi
if [ -n "$UPDATE_LIST" ]; then
    echo "=== Updating modules: $UPDATE_LIST ==="
    EXTRA_ARGS="$EXTRA_ARGS --update $UPDATE_LIST"
fi
if [ -n "$ODOO_INIT_MODULES" ] && [ "$ODOO_INIT_MODULES" != "none" ]; then
    echo "=== Installing modules: $ODOO_INIT_MODULES ==="
    EXTRA_ARGS="$EXTRA_ARGS --init $ODOO_INIT_MODULES"
fi

# ── Start Cloudflare Tunnel (background) ─────────────────────────────
if [ -n "$CLOUDFLARE_TUNNEL_CREDENTIALS" ]; then
    echo "=== Starting Cloudflare Tunnel ==="
    echo "$CLOUDFLARE_TUNNEL_CREDENTIALS" > /etc/cloudflared/credentials.json
    chmod 600 /etc/cloudflared/credentials.json
    cloudflared tunnel --config /etc/cloudflared/config.yml run &
    echo "cloudflared started (pid $!)"
else
    echo "=== Cloudflare Tunnel disabled (CLOUDFLARE_TUNNEL_CREDENTIALS not set) ==="
fi

# ── Patch Odoo to use Redis sessions ────────────────────────────────��
# The mint_redis_session module can't load early enough via server_wide_modules
# because odoo.addons.__path__ isn't populated yet. Instead, we directly patch
# the Application class in odoo/http.py before Odoo starts.
if [ -n "$REDIS_SESSION_URL" ]; then
    echo "=== Patching odoo/http.py for Redis sessions ==="
    ODOO_HTTP="/usr/lib/python3/dist-packages/odoo/http.py"
    if ! grep -q "REDIS_SESSION_PATCH" "$ODOO_HTTP"; then
        cat >> "$ODOO_HTTP" << 'PYREDIS'

# ── REDIS_SESSION_PATCH (injected by fix-config.sh) ──────────────────
def _apply_redis_session_patch():
    import os, logging
    _log = logging.getLogger("mint_redis_session")
    url = os.environ.get("REDIS_SESSION_URL", "")
    if not url:
        return
    try:
        import redis
        _client = redis.Redis.from_url(
            url, decode_responses=True,
            socket_connect_timeout=5, socket_timeout=5, retry_on_timeout=True,
        )
        _client.ping()
    except Exception:
        _log.warning("Redis session: cannot connect, using filesystem", exc_info=True)
        return
    import sys
    sys.path.insert(0, "/opt/extra-addons/mint_redis_session")
    from session import RedisSessionStore
    _store = RedisSessionStore(redis_client=_client)
    Application.session_store = property(lambda self: _store)
    _log.info("Redis session store active: %s", url.split("@")[-1])

_apply_redis_session_patch()
PYREDIS
        echo "Redis session patch injected into odoo/http.py"
    else
        echo "Redis session patch already present"
    fi
fi

# ── MR#680: pre-create res.partner email/call consent columns ───────────────
# Self-contained block with its OWN connection on the correct DB port
# (ODOO_DB_PORT=5432). The older PYCLEAN/PYFIX blocks connect via $PORT (=8080,
# the web port) and fail "Connection refused" — and they also run dormant
# destructive ops, so we do NOT reuse them. -u is unreliable on this prod and
# broke res.partner.create twice; this raw idempotent ALTER (runs every boot,
# before Odoo) guarantees the column exists before the mint_account field code
# loads. Field DEFINITIONS live in mint_account (phase 2).
echo "=== MR#680: pre-creating res.partner consent columns ==="
python3 << 'PYCONSENT' 2>&1
import os, sys
try:
    import psycopg2
except ImportError:
    print("psycopg2 not available, skipping consent column pre-create"); sys.exit(0)
host = os.environ.get("ODOO_DB_HOST") or os.environ.get("HOST", "localhost")
port = os.environ.get("ODOO_DB_PORT") or "5432"
user = os.environ.get("ODOO_DB_USER") or os.environ.get("USER", "odoo")
password = os.environ.get("ODOO_DB_PASSWORD") or os.environ.get("PASSWORD", "")
dbname = os.environ.get("ODOO_DB_NAME", "odoo")
try:
    conn = psycopg2.connect(host=host, port=port, user=user, password=password, dbname=dbname)
    conn.autocommit = True
    cur = conn.cursor()
    for col, coltype in (
        ('email_opt_in', 'boolean'), ('email_opt_in_date', 'timestamp'), ('email_opt_in_source', 'varchar'),
        ('call_opt_in', 'boolean'), ('call_opt_in_date', 'timestamp'), ('call_opt_in_source', 'varchar'),
    ):
        cur.execute("SELECT 1 FROM information_schema.columns WHERE table_name='res_partner' AND column_name=%s", (col,))
        if not cur.fetchone():
            cur.execute(f"ALTER TABLE res_partner ADD COLUMN {col} {coltype}")
            print(f"=== Pre-created column res_partner.{col} ===")
        else:
            print(f"=== res_partner.{col} already exists ===")
    cur.close()
    conn.close()
    print("=== MR#680 consent columns OK ===")
except Exception as e:
    print(f"=== WARNING: consent column pre-create failed: {e} ===")
PYCONSENT

# Execute the original entrypoint
exec /entrypoint.sh "$@" $EXTRA_ARGS
