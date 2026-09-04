# Rolling out receipt printing to a new store register

One command provisions a register end to end. Everything is fetched from the
Odoo host, so a store tech needs only: this repo's `provision-register.ps1`
URL, the Odoo URL, and the store's **node token**.

## Per store, once

1. **Odoo:** Point of Sale -> PrintNodes -> Print Nodes -> **New**. Pick the
   store's company, **Save**, copy the **token** (Rotate Token if needed).
2. **On the register (elevated PowerShell):**
   ```powershell
   powershell -ExecutionPolicy Bypass -Command "irm https://letsgomint.us/print_nodes/static/agent/provision-register.ps1 -OutFile $env:TEMP\prov.ps1; & $env:TEMP\prov.ps1 -OdooUrl https://letsgomint.us -Token <NODE_TOKEN>"
   ```
   That installs Python + SumatraPDF, the Star x64 futurePRNT driver, the
   `Star TSP143` queue, the cash-drawer config, and the print agent (a boot
   service). The node registers itself within ~1 min.
3. **Odoo:** on the node's printer, confirm it shows as a **PDF** receipt
   printer and tick **Default for role** if it is not already.

That's it - POS receipts and online-order receipts then print automatically.

## What makes it durable

- **Agent** runs as a Scheduled Task at boot, as SYSTEM, auto-restart on
  failure - survives reboots and logoffs. Re-running the installer is safe
  (idempotent) and refreshes the agent to the current served version.
- **Driver** is installed into the Windows driver store (survives reboot). The
  x64 futurePRNT driver is bundled (`drivers/startsp_x64.zip`) - no Star
  account or manual download per store.
- **Cash drawer** config is replayed from a captured known-good driver DEVMODE
  (`drivers/star_drawer.json`: Cash Drawer, fire at document bottom). If a
  Windows build rejects the replayed blob the installer says so and prints the
  4-click manual step (Printer properties -> Device Settings).
- **Routing** is by company: each store's node prints its own company's POS and
  online orders; a store with no node simply does not print (safe no-op).

## Hardware notes

- The Star **TSP100 / TSP143 futurePRNT** is raster-only (no ESC/POS): receipts
  are rendered to PDF by Odoo and printed through the OS driver, which is why
  SumatraPDF is required. Odoo auto-classifies these as `printer_lang=pdf`.
- A **StarPRNT** receipt printer (TSP143**III/IV**, mC-Print) or an **Epson TM**
  speaks ESC/POS - it is auto-classified `escpos`, prints natively (faster), and
  fires the drawer from the receipt itself (no driver config needed). A
  **Zebra ZD410** stays `zpl`. Mixed hardware across stores is fine; each
  printer prints in its own language.
- Print **density** on a raster TSP143 is a hardware memory-switch, not a driver
  or software setting - use the Star config utility or darker thermal paper if a
  store needs it darker.
