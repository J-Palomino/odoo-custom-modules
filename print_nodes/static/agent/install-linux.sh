#!/bin/bash
# Install the Mint Print Agent as a systemd service (starts at boot, restarts on crash).
# Usage: sudo ./install-linux.sh <ODOO_URL> <NODE_TOKEN> [DEFAULT_PRINTER]
set -euo pipefail

ODOO_URL="${1:?Usage: install-linux.sh <ODOO_URL> <NODE_TOKEN> [DEFAULT_PRINTER]}"
TOKEN="${2:?NODE_TOKEN required}"
PRINTER="${3:-}"

SRC="$(cd "$(dirname "$0")" && pwd)"
DIR=/opt/mint-print-agent
PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "python3 not found"; exit 1; }

mkdir -p "$DIR"
cp "$SRC/mint_zebra_agent.py" "$DIR/"
cat > "$DIR/mint_print_agent.conf" <<EOF
MINT_ODOO_URL=$ODOO_URL
MINT_NODE_TOKEN=$TOKEN
MINT_AGENT_PRINTER=$PRINTER
MINT_POLL_SECS=2
EOF
chmod 600 "$DIR/mint_print_agent.conf"

cat > /etc/systemd/system/mint-print-agent.service <<EOF
[Unit]
Description=Mint Print Agent
After=network-online.target cups.service
Wants=network-online.target

[Service]
WorkingDirectory=$DIR
ExecStart=$PY $DIR/mint_zebra_agent.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now mint-print-agent
echo "Mint Print Agent installed and started."
echo "  Logs:   journalctl -u mint-print-agent -f"
echo "  Remove: systemctl disable --now mint-print-agent && rm /etc/systemd/system/mint-print-agent.service"
