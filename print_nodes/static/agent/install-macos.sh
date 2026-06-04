#!/bin/bash
# Install the Mint Print Agent as a macOS LaunchAgent (starts at login, restarts on crash).
# Usage: ./install-macos.sh <ODOO_URL> <NODE_TOKEN> [DEFAULT_PRINTER]
#   ODOO_URL       e.g. https://letsgomint.us
#   NODE_TOKEN     from Odoo: Point of Sale > PrintNodes > Print Nodes > (node) > token
#   DEFAULT_PRINTER (optional) OS printer name to use when a job has none
set -euo pipefail

ODOO_URL="${1:?Usage: install-macos.sh <ODOO_URL> <NODE_TOKEN> [DEFAULT_PRINTER]}"
TOKEN="${2:?NODE_TOKEN required}"
PRINTER="${3:-}"

SRC="$(cd "$(dirname "$0")" && pwd)"
DIR="$HOME/Library/Application Support/MintPrintAgent"
PLIST="$HOME/Library/LaunchAgents/com.mint.printagent.plist"

mkdir -p "$DIR"
cp "$SRC/mint_zebra_agent.py" "$DIR/"
cat > "$DIR/mint_print_agent.conf" <<EOF
MINT_ODOO_URL=$ODOO_URL
MINT_NODE_TOKEN=$TOKEN
MINT_AGENT_PRINTER=$PRINTER
MINT_POLL_SECS=2
EOF
chmod 600 "$DIR/mint_print_agent.conf"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.mint.printagent</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>$DIR/mint_zebra_agent.py</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DIR/agent.log</string>
  <key>StandardErrorPath</key><string>$DIR/agent.log</string>
</dict></plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Mint Print Agent installed and started."
echo "  Config: $DIR/mint_print_agent.conf"
echo "  Logs:   $DIR/agent.log"
echo "  Stop/remove: launchctl unload '$PLIST' && rm '$PLIST'"
