# Deploying Custom Odoo Modules on Railway

This document describes how to deploy custom Odoo modules (like `mint_api_v2`) to Odoo 19 running on Railway.

## Overview

The deployment involves:
1. Building a Docker image with custom modules
2. Configuring the Railway service to connect to PostgreSQL
3. Initializing the Odoo database
4. Installing custom modules

## Repository Structure

```
odoo-custom-modules/
├── Dockerfile              # Docker build configuration
├── fix-config.sh           # Startup script to fix volume config
├── odoo.conf               # Backup Odoo configuration
├── avancir_inventory/      # Custom module
└── mint_api_v2/            # Custom REST API module
```

## Key Challenges and Solutions

### 1. Persistent Volume Config Override

**Problem**: Railway mounts a persistent volume at `/var/lib/odoo` which contains an `odoo.conf` file. This file can have incorrect settings (like wrong database port) that override environment variables.

**Solution**: Use a startup script (`fix-config.sh`) that runs before Odoo and corrects the config file on the volume:

```bash
#!/bin/bash
CONFIG_FILE="/var/lib/odoo/odoo.conf"

if [ -f "$CONFIG_FILE" ]; then
    # Fix db_port to 5432 (PostgreSQL default)
    sed -i 's/db_port\s*=\s*[0-9]*/db_port = 5432/g' "$CONFIG_FILE"

    # Add db_port if missing
    if ! grep -q "db_port" "$CONFIG_FILE"; then
        echo "db_port = 5432" >> "$CONFIG_FILE"
    fi
fi

exec /entrypoint.sh "$@"
```

### 2. Database User Security

**Problem**: Odoo refuses to run with the `postgres` superuser for security reasons.

**Solution**: Create a dedicated `odoo` user in PostgreSQL:

```sql
CREATE ROLE odoo WITH LOGIN PASSWORD 'your_password' CREATEDB;
CREATE DATABASE odoo OWNER odoo;
GRANT ALL PRIVILEGES ON DATABASE odoo TO odoo;
```

### 3. Database Initialization

**Problem**: A fresh Odoo database needs to be initialized with the base module before it can be used.

**Solution**: Use `--init base --stop-after-init` on first run:

```bash
odoo -d odoo --init base --stop-after-init && odoo -d odoo
```

### 4. Line Endings (CRLF vs LF)

**Problem**: Shell scripts with Windows-style line endings (CRLF) fail with "required file not found" error.

**Solution**: Ensure all shell scripts use Unix line endings (LF):

```bash
sed -i 's/\r$//' script.sh
```

## Dockerfile

```dockerfile
FROM odoo:19

USER root

# Prepare extra-addons directory
RUN mkdir -p /mnt/extra-addons && rm -rf /mnt/extra-addons/*

# Copy custom modules
COPY --chown=odoo:odoo avancir_inventory /mnt/extra-addons/avancir_inventory
COPY --chown=odoo:odoo mint_api_v2 /mnt/extra-addons/mint_api_v2

# Copy config fix script
COPY fix-config.sh /fix-config.sh
RUN chmod +x /fix-config.sh

USER odoo
ENTRYPOINT ["/fix-config.sh"]
CMD ["odoo"]
```

## Railway Configuration

### Environment Variables

| Variable | Value | Description |
|----------|-------|-------------|
| `HOST` | `postgres.railway.internal` | Internal PostgreSQL hostname |
| `USER` | `odoo` | Database user |
| `PASSWORD` | `<your_password>` | Database password |
| `PORT` | `8069` | Odoo HTTP port |

### Start Command

For initial setup (first deployment):
```
bash -c '/fix-config.sh odoo -d odoo --init base --stop-after-init && /fix-config.sh odoo -d odoo'
```

After database is initialized:
```
/fix-config.sh odoo -d odoo
```

## Installing Custom Modules

### Method 1: Via JSON-RPC API

```bash
# 1. Authenticate
curl -X POST "https://your-odoo-url/jsonrpc" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc": "2.0", "method": "call", "params": {"service": "common", "method": "authenticate", "args": ["odoo", "admin", "admin", {}]}, "id": 1}'

# Returns UID (e.g., 2)

# 2. Update module list
curl -X POST "https://your-odoo-url/jsonrpc" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc": "2.0", "method": "call", "params": {"service": "object", "method": "execute_kw", "args": ["odoo", 2, "admin", "ir.module.module", "update_list", []]}, "id": 2}'

# 3. Find module ID
curl -X POST "https://your-odoo-url/jsonrpc" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc": "2.0", "method": "call", "params": {"service": "object", "method": "execute_kw", "args": ["odoo", 2, "admin", "ir.module.module", "search_read", [[["name", "=", "mint_api_v2"]]], {"fields": ["id", "name", "state"]}]}, "id": 3}'

# 4. Install module (using the returned ID)
curl -X POST "https://your-odoo-url/jsonrpc" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc": "2.0", "method": "call", "params": {"service": "object", "method": "execute_kw", "args": ["odoo", 2, "admin", "ir.module.module", "button_immediate_install", [[MODULE_ID]]]}, "id": 4}'
```

### Method 2: Via Odoo Web Interface

1. Navigate to `https://your-odoo-url/web`
2. Log in as admin
3. Go to Apps menu
4. Click "Update Apps List"
5. Search for your module
6. Click Install

### Method 3: Via Start Command

Add module to init on startup (only works with fresh database):
```
/fix-config.sh odoo -d odoo -i mint_api_v2
```

## Testing the API

After installation, test the mint_api_v2 endpoints:

```bash
# List stores
curl -H "X-Odoo-Database: odoo" https://your-odoo-url/api/v1/stores

# List products
curl -H "X-Odoo-Database: odoo" https://your-odoo-url/api/v1/products

# List blog posts
curl -H "X-Odoo-Database: odoo" https://your-odoo-url/api/v1/blog
```

## Troubleshooting

### "connection to server on socket" error
- Cause: Database port is wrong (often 8069 instead of 5432)
- Fix: Ensure `fix-config.sh` is running and correcting the port

### "password authentication failed for user"
- Cause: User doesn't exist or wrong password
- Fix: Create the user in PostgreSQL with correct password

### "Database not initialized"
- Cause: Fresh database without Odoo tables
- Fix: Run with `--init base --stop-after-init`

### "Using the database user 'postgres' is a security risk"
- Cause: Trying to connect as superuser
- Fix: Create a non-superuser database user

### Module not appearing in Apps list
- Cause: Module not in addons path or syntax error
- Fix: Check `/mnt/extra-addons` is in addons_path, verify `__manifest__.py`

## Railway Deployment Commands

Deploy from specific commit:
```bash
# Using Railway GraphQL API
scripts/railway-api.sh \
  'mutation patchCommit($environmentId: String!, $patch: EnvironmentConfig, $commitMessage: String) {
    environmentPatchCommit(environmentId: $environmentId, patch: $patch, commitMessage: $commitMessage)
  }' \
  '{"environmentId": "ENV_ID", "patch": {"services": {"SERVICE_ID": {"source": {"commitSHA": "COMMIT_HASH"}}}}, "commitMessage": "Deploy update"}'
```

## References

- [Odoo Docker Documentation](https://hub.docker.com/_/odoo)
- [Railway PostgreSQL Guide](https://docs.railway.app/databases/postgresql)
- [Odoo Module Development](https://www.odoo.com/documentation/19.0/developer/tutorials/backend.html)
