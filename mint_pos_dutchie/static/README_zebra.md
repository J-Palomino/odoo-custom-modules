# Zebra ZD410 local printing (Odoo POS)

Lets the **native Odoo POS register** (this `mint_pos_dutchie` module) print
**exit labels** and **receipts** to a **Zebra ZD410** attached to the machine
running the POS in the browser/PWA.

The ZD410 speaks **ZPL**, not ESC/POS — so Odoo's built-in ePOS / Star / IoT
printer paths cannot drive it. This module generates ZPL server-side and sends
it via one of two transports.

## How it works

```
POS receipt screen (browser)
   │  on payment (auto) or "Print Receipt" click
   ▼
mint_zebra service ── auto ──► WebUSB ──────────────► ZD410 (USB)   [client-side]
                          └──► PrintNode (fallback) ─► ZD410         [server-side]
                                   ▲
pos.order.get_mint_zebra_zpl   ────┘  ZPL built from ORM fields (zebra_zpl.py)
pos.order.mint_printnode_print ──► POST api.printnode.com/printjobs (key stays on server)
```

- **WebUSB** — direct from the browser to a USB ZD410 (Zebra vendorId `0x0A5F`).
  Chromium-only, HTTPS required (POS is). First pairing needs a click (the
  "Print Receipt" button); after that it prints silently.
- **PrintNode** — the PrintNode client on the register exposes the ZD410 (via
  its normal OS driver) to the PrintNode cloud; Odoo POSTs base64 ZPL with
  `contentType: raw_base64`. The **API key never reaches the browser** (it lives
  in a System Parameter) and there is no CORS / localhost-cert friction.
- **auto** — try the paired USB printer, fall back to PrintNode.

## Per-register configuration

Point of Sale ▸ Configuration ▸ *(your register)* ▸ **Zebra ZD410 Local Printing**:

| Field | Notes |
|-------|-------|
| Enable Zebra ZD410 Printing | master switch for this register |
| Zebra Transport | `auto` / `WebUSB only` / `PrintNode only` |
| Zebra DPI | `203` (standard ZD410) or `300` |
| Auto-print on payment | print automatically when the receipt screen opens |
| Print Exit Label / Print Receipt | what to print |
| PrintNode Printer ID | numeric id from PrintNode `GET /printers` (PrintNode transports only) |

## Global setup (once)

1. **Install / upgrade** `mint_pos_dutchie` (custom modules do **not** auto-deploy
   — trigger an upgrade: `-u mint_pos_dutchie` or the Apps UI).
2. **PrintNode** (for the cloud transport): install the PrintNode client on each
   register, add the ZD410, note its printer id, and set the API key system
   parameter:
   - Settings ▸ Technical ▸ System Parameters →
     `mint_pos_dutchie.printnode_api_key` = `<your PrintNode API key>`
3. **WebUSB on Windows**: the OS print queue can hold the USB interface and make
   `claimInterface` fail. Either use the **PrintNode** transport on Windows, or
   replace the printer driver with **WinUSB via Zadig** so Chrome can claim it.
   macOS/Linux generally work without changes.

## Verify after install

- Confirm POS config fields reach the frontend: `_load_pos_data_fields` on
  `pos.config` must include the `mint_zebra_*` fields (Odoo 19 hook). If the
  fields are undefined in the POS, printing silently no-ops — re-check this hook
  for your pinned Odoo 19 build.
- ZPL preview without hardware:
  `env['pos.order'].browse(<id>)._mint_zebra_zpl('both')`.
- Test print: open the receipt screen and tap **Print Receipt** (pairs WebUSB),
  or set Transport = PrintNode and watch a job appear in the PrintNode dashboard.
