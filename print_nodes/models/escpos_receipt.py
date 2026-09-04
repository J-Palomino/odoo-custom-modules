# -*- coding: utf-8 -*-
"""
ESC/POS receipt generator for thermal receipt printers (Star TSP-series,
Epson TM-series, Citizen, mPOP, ...).

These printers speak ESC/POS, NOT ZPL — the opposite of the Zebra ZD410 that
``zebra_zpl.py`` targets. We emit an ESC/POS byte stream here from the same
server-side order dict ``pos_order._mint_zebra_data`` produces, and it is sent
to the printer as RAW bytes by the store agent (winspool RAW / CUPS lp -o raw).

Why bytes, not text: ESC/POS commands include NUL and other control bytes that
a PostgreSQL text column rejects, so a receipt built here is carried to the
agent base64-encoded in the job's binary ``pdf_data`` field under
``doc_type='escpos'`` — never in the ``zpl`` text field. See
``print_node.enqueue_pos``.

Pure functions, no ORM access — easy to unit-test and py_compile.

Layout targets an 80 mm printer (48 columns at Font A); ``cols`` is parametric
for 58 mm media (32 columns).
"""

# ── ESC/POS command bytes ────────────────────────────────────────────
ESC = b'\x1b'
GS = b'\x1d'

INIT = ESC + b'@'                       # ESC @  — initialize / reset
ALIGN_L = ESC + b'a' + b'\x00'          # ESC a 0
ALIGN_C = ESC + b'a' + b'\x01'          # ESC a 1
ALIGN_R = ESC + b'a' + b'\x02'          # ESC a 2
BOLD_ON = ESC + b'E' + b'\x01'          # ESC E 1
BOLD_OFF = ESC + b'E' + b'\x00'         # ESC E 0
SIZE_NORMAL = GS + b'!' + b'\x00'       # GS ! 0   — 1x1
SIZE_DWDH = GS + b'!' + b'\x11'         # GS ! 0x11 — 2x wide, 2x tall
FEED = b'\n'


def _feed(n):
    return ESC + b'd' + bytes([max(0, min(255, int(n)))])   # ESC d n


def _cut():
    # GS V 66 n — partial cut, feeding n dots first so the cut clears the text.
    return GS + b'V' + b'\x42' + b'\x10'


def _drawer_kick():
    # ESC p m t1 t2 — pulse drawer pin 2 (m=0), on 50ms (25*2), off 500ms.
    return ESC + b'p' + b'\x00' + b'\x19' + b'\xfa'


def _enc(text):
    """Encode display text to CP437 (the ESC/POS default code page)."""
    if text is None:
        text = ''
    return str(text).encode('cp437', 'replace')


def _money(value, currency):
    try:
        return '%s%0.2f' % (currency, float(value))
    except (TypeError, ValueError):
        return '%s0.00' % currency


def _two_col(left, right, cols):
    """Left text + right-aligned value on one line of ``cols`` columns.

    The value is never truncated; the label is trimmed to make room, and if
    even the value alone overflows it is placed on its own line.
    """
    left = '' if left is None else str(left)
    right = '' if right is None else str(right)
    if len(right) >= cols:
        return right[:cols] + '\n'
    room = cols - len(right)
    if len(left) > room - 1:
        left = left[:max(0, room - 2)] + ('…' if room >= 2 else '')
    pad = cols - len(left) - len(right)
    return left + (' ' * max(1, pad)) + right + '\n'


def _center(text, cols):
    text = '' if text is None else str(text)
    if len(text) >= cols:
        return text[:cols] + '\n'
    pad = (cols - len(text)) // 2
    return (' ' * pad) + text + '\n'


def _wrap(text, cols):
    """Greedy word-wrap to ``cols``; always returns at least one line."""
    words = str(text or '').split()
    if not words:
        return ['']
    lines, cur = [], ''
    for w in words:
        if len(w) > cols:                      # hard-break an over-long token
            if cur:
                lines.append(cur)
                cur = ''
            while len(w) > cols:
                lines.append(w[:cols])
                w = w[cols:]
            cur = w
        elif not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= cols:
            cur += ' ' + w
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def build_receipt_escpos(data, cols=48, open_drawer=False, cut=True):
    """Return an ESC/POS byte string for an itemized sales receipt.

    ``data`` keys: store, address, order_ref, date, cashier, customer,
    items [{name, qty, price, total}], item_count, subtotal, tax, total,
    currency, footer.

    ``open_drawer`` appends a drawer-kick pulse (cash sales); ``cut`` appends a
    partial cut. ``cols`` is the printable width in characters (48 for 80 mm,
    32 for 58 mm).
    """
    cur = data.get('currency') or '$'
    rule = ('-' * cols) + '\n'
    out = [INIT]

    # ── header ──
    out.append(ALIGN_C)
    out.append(BOLD_ON + SIZE_DWDH)
    out.append(_enc(_center(data.get('store') or '', max(1, cols // 2)).rstrip('\n') + '\n'))
    out.append(SIZE_NORMAL + BOLD_OFF)
    for ln in _wrap(data.get('address') or '', cols):
        if ln:
            out.append(_enc(ln + '\n'))
    out.append(ALIGN_L)
    out.append(_enc(rule))

    # ── meta ──
    if data.get('order_ref'):
        out.append(BOLD_ON + _enc(str(data.get('order_ref'))[:cols] + '\n') + BOLD_OFF)
    if data.get('date'):
        out.append(_enc(str(data.get('date'))[:cols] + '\n'))
    if data.get('cashier'):
        out.append(_enc(('Cashier: ' + str(data.get('cashier')))[:cols] + '\n'))
    if data.get('customer'):
        out.append(_enc(('Customer: ' + str(data.get('customer')))[:cols] + '\n'))
    out.append(_enc(rule))

    # ── items ──  name on its own line, then "qty x price ... line-total"
    for item in (data.get('items') or []):
        name = item.get('name') or ''
        for ln in _wrap(name, cols):
            out.append(_enc(ln + '\n'))
        try:
            qty = float(item.get('qty') or 0)
            qty_s = ('%g' % qty)
        except (TypeError, ValueError):
            qty_s = str(item.get('qty') or '')
        left = '  %s x %s' % (qty_s, _money(item.get('price'), cur))
        out.append(_enc(_two_col(left, _money(item.get('total'), cur), cols)))
    out.append(_enc(rule))

    # ── totals ──
    out.append(_enc(_two_col('Subtotal', _money(data.get('subtotal'), cur), cols)))
    out.append(_enc(_two_col('Tax', _money(data.get('tax'), cur), cols)))
    if data.get('item_count') is not None:
        out.append(_enc(_two_col('Items', str(data.get('item_count')), cols)))
    out.append(BOLD_ON + SIZE_DWDH)
    # double-width halves the column count for this line
    out.append(_enc(_two_col('TOTAL', _money(data.get('total'), cur), max(1, cols // 2))))
    out.append(SIZE_NORMAL + BOLD_OFF)

    # ── footer ──
    if data.get('footer'):
        out.append(_enc(rule))
        out.append(ALIGN_C)
        for ln in _wrap(data.get('footer'), cols):
            out.append(_enc(ln + '\n'))
        out.append(ALIGN_L)

    out.append(_feed(4))
    if open_drawer:
        out.append(_drawer_kick())
    if cut:
        out.append(_cut())
    return b''.join(out)
