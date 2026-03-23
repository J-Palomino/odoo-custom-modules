#!/bin/sh
# Start Cloudflare Tunnel as a background process.
# Tunnel credentials come from CLOUDFLARE_TUNNEL_CREDENTIALS env var (JSON string).
# If not set, tunnel is skipped (Odoo still works without it).

if [ -z "$CLOUDFLARE_TUNNEL_CREDENTIALS" ]; then
  echo "[cloudflared] CLOUDFLARE_TUNNEL_CREDENTIALS not set — tunnel disabled"
  exit 0
fi

# Write credentials from env var to file
echo "$CLOUDFLARE_TUNNEL_CREDENTIALS" > /etc/cloudflared/credentials.json
chmod 600 /etc/cloudflared/credentials.json

echo "[cloudflared] Starting tunnel..."
exec cloudflared tunnel --config /etc/cloudflared/config.yml run
