# -*- coding: utf-8 -*-
"""
ZPL II generators for the Zebra ZD410 (direct-thermal label printer).

The ZD410 speaks ZPL/EPL, NOT ESC/POS, so Odoo's built-in ePOS/Star/IoT
raster printer paths cannot drive it. We build ZPL command strings here
from reliable server-side ORM data and send them to the printer either via
the browser (WebUSB) or via PrintNode (see pos_order.py).

Pure functions, no ORM access — easy to unit-test and py_compile.

Reference: 203 dpi => 8 dots/mm. The ZD410 is a ~2.25" wide printer; we
default the print width to 2.0" of media.
"""


def _dots(inches, dpi):
    return int(round(inches * dpi))


def _sanitize(value):
    """Strip ZPL control characters from user/data text.

    ``^`` and ``~`` are ZPL command prefixes and ``\\`` is the escape
    character; a literal occurrence inside ^FD corrupts the format. We
    drop them rather than hex-escape to keep the output readable.
    """
    if not value:
        return ''
    return (
        str(value)
        .replace('\\', ' ')
        .replace('^', ' ')
        .replace('~', ' ')
        .strip()
    )


def _truncate(value, max_chars):
    value = _sanitize(value)
    if len(value) > max_chars:
        return value[: max_chars - 1] + '…'
    return value


def build_label_zpl(data, dpi=203):
    """Cannabis exit / compliance label.

    ``data`` keys used: store, order_ref, date, customer, barcode, item_count.
    """
    width = _dots(2.0, dpi)
    f = 28 if dpi == 203 else 42   # base font height in dots
    s = 22 if dpi == 203 else 33   # small font height in dots

    lines = [
        '^XA',
        '^CI28',                       # UTF-8
        f'^PW{width}',                 # print width
        '^LH0,0',
        f'^FO16,18^A0N,{f},{f}^FD{_truncate(data.get("store"), 26)}^FS',
        f'^FO16,{18 + f + 8}^A0N,{s},{s}^FDEXIT LABEL^FS',
        f'^FO16,{18 + f + 8 + s + 6}^A0N,{s},{s}^FD{_truncate(data.get("order_ref"), 26)}^FS',
        f'^FO16,{18 + f + 8 + (s + 6) * 2}^A0N,{s},{s}^FD{_sanitize(data.get("date"))}^FS',
    ]
    customer = data.get('customer')
    y = 18 + f + 8 + (s + 6) * 3
    if customer:
        lines.append(f'^FO16,{y}^A0N,{s},{s}^FD{_truncate(customer, 26)}^FS')
        y += s + 6
    item_count = data.get('item_count')
    if item_count is not None:
        lines.append(f'^FO16,{y}^A0N,{s},{s}^FDItems: {item_count}^FS')
        y += s + 10

    barcode = _sanitize(data.get('barcode')) or _sanitize(data.get('order_ref'))
    if barcode:
        bc_h = 70 if dpi == 203 else 104
        lines.append(f'^FO16,{y}^BY2^BCN,{bc_h},Y,N,N^FD{barcode}^FS')

    lines.append('^XZ')
    return '\n'.join(lines)


def build_receipt_zpl(data, dpi=203):
    """Condensed itemized sales receipt rendered on the label roll.

    ``data`` keys used: store, address, order_ref, date, cashier, customer,
    items (list of {name, qty, price, total}), subtotal, tax, total,
    currency, footer, barcode.
    """
    width = _dots(2.0, dpi)
    body = 20 if dpi == 203 else 30          # body font height
    head = 26 if dpi == 203 else 39          # header font height
    line_h = body + 6
    pad_x = 12
    text_chars = 30 if dpi == 203 else 30    # chars per line at body font

    cur = data.get('currency') or '$'

    def money(v):
        try:
            return f'{cur}{float(v):,.2f}'
        except (TypeError, ValueError):
            return f'{cur}0.00'

    rows = []
    y = 16

    def add(text, font=body, indent=0):
        nonlocal y
        rows.append(f'^FO{pad_x + indent},{y}^A0N,{font},{font}^FD{text}^FS')
        y += (font + 6)

    def rule():
        nonlocal y
        rows.append(f'^FO{pad_x},{y}^GB{width - pad_x * 2},2,2^FS')
        y += 10

    add(_truncate(data.get('store'), 24), font=head)
    if data.get('address'):
        add(_truncate(data.get('address'), text_chars))
    add(_truncate(data.get('order_ref'), text_chars))
    add(_sanitize(data.get('date')))
    if data.get('cashier'):
        add(f'Cashier: {_truncate(data.get("cashier"), 22)}')
    if data.get('customer'):
        add(f'Customer: {_truncate(data.get("customer"), 20)}')
    rule()

    for item in data.get('items', []):
        name = _truncate(item.get('name'), text_chars)
        add(name)
        qty = item.get('qty')
        price = money(item.get('price'))
        total = money(item.get('total'))
        # qty x unit .......... line total
        left = f'  {qty} x {price}'
        rows.append(f'^FO{pad_x},{y}^A0N,{body},{body}^FD{left}^FS')
        rows.append(
            f'^FO{pad_x},{y}^A0N,{body},{body}^FB{width - pad_x * 2},1,0,R^FD{total}^FS'
        )
        y += line_h
    rule()

    def total_row(label, value, font=body):
        nonlocal y
        rows.append(f'^FO{pad_x},{y}^A0N,{font},{font}^FD{label}^FS')
        rows.append(
            f'^FO{pad_x},{y}^A0N,{font},{font}^FB{width - pad_x * 2},1,0,R^FD{value}^FS'
        )
        y += (font + 8)

    total_row('Subtotal', money(data.get('subtotal')))
    total_row('Tax', money(data.get('tax')))
    total_row('TOTAL', money(data.get('total')), font=head)
    y += 8

    footer = data.get('footer')
    if footer:
        for chunk in str(footer).splitlines():
            add(_truncate(chunk, text_chars))

    barcode = _sanitize(data.get('barcode')) or _sanitize(data.get('order_ref'))
    if barcode:
        bc_h = 60 if dpi == 203 else 90
        rows.append(f'^FO{pad_x},{y}^BY2^BCN,{bc_h},Y,N,N^FD{barcode}^FS')
        y += bc_h + 20

    label_len = y + 20
    zpl = ['^XA', '^CI28', f'^PW{width}', f'^LL{label_len}', '^LH0,0']
    zpl.extend(rows)
    zpl.append('^XZ')
    return '\n'.join(zpl)
