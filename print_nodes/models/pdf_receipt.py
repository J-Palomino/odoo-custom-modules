# -*- coding: utf-8 -*-
"""
PDF receipt generator for raster receipt printers driven by their OS driver.

Some receipt printers speak neither ZPL nor ESC/POS: the Star TSP100 / TSP143
"futurePRNT" is raster-only — it prints only bitmaps its own Windows driver
rasterises, and silently discards raw ZPL or ESC/POS command bytes (verified on
a live TSP143: a raw ESC/POS job was accepted by the spooler and produced
nothing, drawer included). For those printers the receipt is rendered to a PDF
here and printed through the OS driver (agent doc_type='pdf', SumatraPDF /
GhostScript on Windows), which the raster driver turns into dots.

The cash drawer on this path is NOT fired from the receipt content (there are no
ESC/POS bytes) — it is opened by the printer driver's own "open cash drawer on
print" setting, configured on the register.

Renders an 80 mm-wide (203 pt printable) continuous receipt whose height grows
with the order. Uses reportlab, which Odoo already depends on.

Pure function apart from reportlab; no ORM access.
"""
from io import BytesIO

from reportlab.lib.units import mm
from reportlab.pdfgen import canvas


def _money(value, currency):
    try:
        return '%s%0.2f' % (currency, float(value))
    except (TypeError, ValueError):
        return '%s0.00' % currency


def _wrap(text, font, size, max_w, c):
    """Word-wrap ``text`` to ``max_w`` points at the given font; >=1 line."""
    words = str(text or '').split()
    if not words:
        return ['']
    lines, cur = [], ''
    for w in words:
        trial = (cur + ' ' + w).strip()
        if c.stringWidth(trial, font, size) <= max_w or not cur:
            # hard-break a single token wider than the column
            if not cur and c.stringWidth(w, font, size) > max_w:
                piece = ''
                for ch in w:
                    if c.stringWidth(piece + ch, font, size) <= max_w:
                        piece += ch
                    else:
                        lines.append(piece)
                        piece = ch
                cur = piece
            else:
                cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def build_receipt_pdf(data, width_mm=80.0):
    """Return receipt PDF bytes sized for an 80 mm (or ``width_mm``) roll.

    ``data`` keys: store, address, order_ref, date, cashier, customer,
    items [{name, qty, price, total}], item_count, subtotal, tax, total,
    currency, footer.
    """
    cur = data.get('currency') or '$'
    page_w = width_mm * mm
    margin = 4 * mm
    inner = page_w - 2 * margin
    F = 'Helvetica'
    FB = 'Helvetica-Bold'
    body, small, big = 8.5, 7.5, 12

    # ---- first pass: lay the receipt out into (font, size, kind, payload) ops
    # so we can measure total height, then render onto a page of that height.
    line_h = body + 3.0
    small_h = small + 2.5
    ops = []          # ('text', font, size, x-align, text) | ('rule',) | ('gap', h) | ('row', left, right)

    def add_center(text, font, size):
        for ln in _need_wrap(text, font, size):
            ops.append(('text', font, size, 'c', ln))

    def _need_wrap(text, font, size):
        # measured against a temp canvas below; deferred via closure list
        return _wrap(text, font, size, inner, _meas)

    # a throwaway canvas purely for string width measurement
    _meas = canvas.Canvas(BytesIO(), pagesize=(page_w, 10 * mm))

    # header
    add_center(data.get('store') or '', FB, big)
    for ln in _wrap(data.get('address') or '', F, small, inner, _meas):
        if ln:
            ops.append(('text', F, small, 'c', ln))
    ops.append(('rule',))
    # meta
    if data.get('order_ref'):
        ops.append(('text', FB, body, 'l', str(data.get('order_ref'))))
    if data.get('date'):
        ops.append(('text', F, small, 'l', str(data.get('date'))))
    if data.get('cashier'):
        ops.append(('text', F, small, 'l', 'Cashier: ' + str(data.get('cashier'))))
    if data.get('customer'):
        ops.append(('text', F, small, 'l', 'Customer: ' + str(data.get('customer'))))
    ops.append(('rule',))
    # items
    for it in (data.get('items') or []):
        for ln in _wrap(it.get('name') or '', F, body, inner, _meas):
            ops.append(('text', F, body, 'l', ln))
        try:
            qty_s = '%g' % float(it.get('qty') or 0)
        except (TypeError, ValueError):
            qty_s = str(it.get('qty') or '')
        ops.append(('row', F, body, '  %s x %s' % (qty_s, _money(it.get('price'), cur)),
                    _money(it.get('total'), cur)))
    ops.append(('rule',))
    # totals
    ops.append(('row', F, body, 'Subtotal', _money(data.get('subtotal'), cur)))
    ops.append(('row', F, body, 'Tax', _money(data.get('tax'), cur)))
    if data.get('item_count') is not None:
        ops.append(('row', F, body, 'Items', str(data.get('item_count'))))
    ops.append(('row', FB, big, 'TOTAL', _money(data.get('total'), cur)))
    # footer
    if data.get('footer'):
        ops.append(('rule',))
        for ln in _wrap(data.get('footer'), F, small, inner, _meas):
            ops.append(('text', F, small, 'c', ln))

    def op_height(op):
        if op[0] == 'rule':
            return 5.0
        if op[0] == 'gap':
            return op[1]
        size = op[2]
        return size + 3.0

    content_h = sum(op_height(o) for o in ops)
    page_h = content_h + 2 * margin + 8 * mm   # trailing feed so the cut clears

    # ---- second pass: render ----
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(page_w, page_h))
    y = page_h - margin

    def draw_text(font, size, align, text):
        nonlocal y
        y -= size + 3.0
        c.setFont(font, size)
        if align == 'c':
            c.drawCentredString(page_w / 2.0, y, text)
        else:
            c.drawString(margin, y, text)

    def draw_row(font, size, left, right):
        nonlocal y
        y -= size + 3.0
        c.setFont(font, size)
        c.drawString(margin, y, left)
        c.drawRightString(page_w - margin, y, right)

    def draw_rule():
        nonlocal y
        y -= 5.0
        c.setLineWidth(0.5)
        c.line(margin, y + 2, page_w - margin, y + 2)

    for op in ops:
        if op[0] == 'text':
            draw_text(op[1], op[2], op[3], op[4])
        elif op[0] == 'row':
            draw_row(op[1], op[2], op[3], op[4])
        elif op[0] == 'rule':
            draw_rule()

    c.showPage()
    c.save()
    return buf.getvalue()
