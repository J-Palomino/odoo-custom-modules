# Mint Print Agent — install on a store machine

The agent connects one machine at a store (the **print node**) to PrintNodes in
Odoo. It registers the machine's printers, polls the Odoo job queue, and prints
jobs to the OS printers (Zebra ZPL **and** PDF→any printer via the OS driver).
Pure Python 3 standard library — **no pip install needed**.

## Before you start

1. In Odoo: **Point of Sale → PrintNodes → Print Nodes → New**, pick the store
   company, **Save**, then copy the node **token** (Rotate Token if needed).
2. Decide the **Odoo URL** — production is `https://letsgomint.us`.
3. Download this `agent/` folder onto the register
   (`https://letsgomint.us/print_nodes/static/agent/`).

## Install (runs as a background service, starts on boot/login, auto-restarts)

**macOS**
```bash
chmod +x install-macos.sh
./install-macos.sh https://letsgomint.us <NODE_TOKEN> "ZD410_LGM"
```

**Linux**
```bash
sudo ./install-linux.sh https://letsgomint.us <NODE_TOKEN> "Zebra_ZD410"
```

**Windows** (elevated PowerShell)
```powershell
powershell -ExecutionPolicy Bypass -File install-windows.ps1 `
    -OdooUrl https://letsgomint.us -Token <NODE_TOKEN> -Printer "ZDesigner ZD410"
```

The third/`-Printer` argument is the **default** printer (optional) — the OS
printer name used when a job doesn't name one. All printers are auto-registered
regardless; set each one's role/default in Odoo.

## Config

The installers write `mint_print_agent.conf` next to the agent:
```
MINT_ODOO_URL=https://letsgomint.us
MINT_NODE_TOKEN=...
MINT_AGENT_PRINTER=ZD410_LGM
MINT_POLL_SECS=2
```
Edit it and restart the service to change settings.

## PDF printing prerequisites

- **macOS / Linux**: works out of the box (CUPS renders PDFs).
- **Windows**: install **GhostScript** (`gswin64c.exe` on PATH) **or**
  **SumatraPDF** — needed to render PDFs to a printer. Zebra/ZPL needs neither.

## Standalone Windows .exe (no Python install)

On a Windows box: `pip install pyinstaller && pyinstaller mint_print_agent.spec`
→ `dist/mint_print_agent.exe`. Drop it + `mint_print_agent.conf` on the
register and point the Scheduled Task at the exe instead of `python`.

## Verify

After install, the node shows **Online** in Odoo (Point of Sale → PrintNodes →
Print Nodes) and its printers appear. Test from **PrintNodes → Print a Document**
(PDF) or the **Label Designer → Print to Node** (Zebra).

## Uninstall

- macOS: `launchctl unload ~/Library/LaunchAgents/com.mint.printagent.plist && rm ~/Library/LaunchAgents/com.mint.printagent.plist`
- Linux: `sudo systemctl disable --now mint-print-agent && sudo rm /etc/systemd/system/mint-print-agent.service`
- Windows: `Unregister-ScheduledTask -TaskName MintPrintAgent -Confirm:$false`
