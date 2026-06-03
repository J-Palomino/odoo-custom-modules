# -*- coding: utf-8 -*-
"""
ZPL II generators for the Zebra ZD410 (direct-thermal label printer).

The ZD410 speaks ZPL/EPL, NOT ESC/POS, so Odoo's built-in ePOS/Star/IoT
raster printer paths cannot drive it. We build ZPL command strings here from
reliable server-side ORM data and send them to the printer either via the
browser (WebUSB) or via PrintNode (see pos_order.py).

Both transports deliver the ZPL as raw bytes straight to the printer, so the
labels are self-configuring: each one emits gap sensing (^MNY), direct-thermal
media type (^MTD), darkness (^MD), print width (^PW) and label length (^LL).

Layout is designed for 2" x 4" die-cut labels at 203 dpi (406 x 812 dots),
the stock validated on real hardware 2026-06-02. Dimensions are parametric
(width_in / length_in / dpi) for stores on different media.

Pure functions, no ORM access — easy to unit-test and py_compile.
203 dpi => 8 dots/mm.
"""


def _dots(inches, dpi):
    return int(round(inches * dpi))


def _scaler(dpi):
    """Return a function scaling a 203-dpi design value to the target dpi."""
    k = dpi / 203.0
    return lambda v: int(round(v * k))


def _sanitize(value):
    """Strip ZPL control characters (^ ~ \\) from data text."""
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


def _media_header(width, length, darkness, media):
    head = ['^XA', '^CI28']
    if media:
        head += ['^MNY', '^MTD', '^MD%d' % int(darkness)]
    head += ['^PW%d' % width, '^LL%d' % length, '^LS0', '^LH0,0']
    return head


def build_label_zpl(data, dpi=203, width_in=2.0, length_in=4.0,
                    darkness=20, media=True):
    """Cannabis exit / compliance label, sized to fill a 2" x 4" label.

    ``data`` keys: store, order_ref, date, customer, barcode, item_count.
    """
    d = _scaler(dpi)
    width = _dots(width_in, dpi)
    length = _dots(length_in, dpi)
    mx = d(20)
    fw = width - 2 * mx

    L = _media_header(width, length, darkness, media)

    # Store name — large, centered, up to 2 lines
    L.append('^FO%d,%d^A0N,%d,%d^FB%d,2,0,C^FD%s^FS'
             % (mx, d(34), d(54), d(54), fw, _truncate(data.get('store'), 22)))
    L.append('^FO%d,%d^GB%d,%d,%d^FS' % (mx, d(160), fw, d(3), d(3)))
    L.append('^FO%d,%d^A0N,%d,%d^FB%d,1,0,C^FDEXIT LABEL^FS'
             % (mx, d(178), d(48), d(48), fw))

    y = 250
    L.append('^FO%d,%d^A0N,%d,%d^FD%s^FS'
             % (mx, d(y), d(36), d(36), _truncate(data.get('order_ref'), 22)))
    y += 56
    L.append('^FO%d,%d^A0N,%d,%d^FD%s^FS'
             % (mx, d(y), d(32), d(32), _sanitize(data.get('date'))))
    y += 56
    if data.get('customer'):
        L.append('^FO%d,%d^A0N,%d,%d^FD%s^FS'
                 % (mx, d(y), d(34), d(34), _truncate(data.get('customer'), 22)))
        y += 56
    item_count = data.get('item_count')
    if item_count is not None:
        L.append('^FO%d,%d^A0N,%d,%d^FDItems: %s^FS'
                 % (mx, d(y), d(32), d(32), item_count))
        y += 56

    L.append('^FO%d,%d^GB%d,%d,%d^FS' % (mx, d(y + 6), fw, d(3), d(3)))
    y += 40

    barcode = _sanitize(data.get('barcode')) or _sanitize(data.get('order_ref'))
    if barcode:
        L.append('^FO%d,%d^BY3^BCN,%d,Y,N,N^FD%s^FS'
                 % (mx, d(y), d(210), barcode))

    L.append('^XZ')
    return '\n'.join(L)


def build_product_label_zpl(data, dpi=203, width_in=2.0, length_in=4.0,
                            darkness=20, media=True):
    """Cannabis product / compliance (METRC) label, portrait 2" x 4".

    ``data`` keys: store, address, product_name, lot, pkg_date, use_by,
    thc, cbd, thca, cbda, lab, test_date, harvest_date, ingredients,
    usable_weight, microbials, mycotoxins, solvents, pesticides, url, barcode.

    NOTE: a full METRC tag is 24 chars; a Code128 of that at scannable module
    width is wider than 2". Tune the barcode / media in the label designer.
    """
    d = _scaler(dpi)
    width = _dots(width_in, dpi)
    length = _dots(length_in, dpi)
    mx = d(12)
    fw = width - 2 * mx
    g = data.get

    def line(y, h, s, fb=False, just='L'):
        if fb:
            return ('^FO%d,%d^A0N,%d,%d^FB%d,3,0,%s^FD%s^FS'
                    % (mx, d(y), d(h), d(h), fw, just, s))
        return '^FO%d,%d^A0N,%d,%d^FD%s^FS' % (mx, d(y), d(h), d(h), s)

    def rule(y):
        return '^FO%d,%d^GB%d,%d,%d^FS' % (mx, d(y), fw, d(2), d(2))

    def pct(label, value):
        return '%s:%s' % (label, _sanitize(value)) if value not in (None, '') else label

    L = _media_header(width, length, darkness, media)
    L.append(line(10, 18, _truncate(g('store_line') or g('address'), 90), fb=True))
    L.append(line(58, 30, _truncate(g('product_name'), 44), fb=True))
    L.append(rule(126))

    y = 140
    rows = [
        'Lot: %s' % _sanitize(g('lot')),
        'Pkg:%s   Use By:%s' % (_sanitize(g('pkg_date')), _sanitize(g('use_by'))),
        '%s    %s' % (pct('THC', g('thc')), pct('CBD', g('cbd'))),
        '%s   %s' % (pct('THCA', g('thca')), pct('CBDA', g('cbda'))),
        'Lab: %s' % _sanitize(g('lab')),
        'Test:%s  Harvest:%s' % (_sanitize(g('test_date')), _sanitize(g('harvest_date'))),
        'Ingredients: %s' % _truncate(g('ingredients'), 34),
    ]
    for s in rows:
        L.append(line(y, 19, s))
        y += 28
    L.append(rule(y + 2))
    y += 14

    for s in [
        'Usable: %s' % _sanitize(g('usable_weight')),
        'Microbials:%s   Mycotoxins:%s' % (_sanitize(g('microbials')), _sanitize(g('mycotoxins'))),
        'Solvents:%s     Pesticides:%s' % (_sanitize(g('solvents')), _sanitize(g('pesticides'))),
    ]:
        L.append(line(y, 18, s))
        y += 28
    if g('url'):
        L.append(line(y + 6, 18, _truncate(g('url'), 36)))
        y += 34

    barcode = _sanitize(g('barcode'))
    if barcode:
        L.append('^FO%d,%d^BY2^BCN,%d,Y,N,N^FD%s^FS' % (mx, d(y + 10), d(150), barcode))

    L.append('^XZ')
    return '\n'.join(L)


def build_receipt_zpl(data, dpi=203, width_in=2.0, length_in=4.0,
                      darkness=20, media=True):
    """Condensed itemized sales receipt, sized for 2"-wide label media.

    Fills at least ``length_in`` and grows downward for long orders (the gap
    sensor indexes onto the next physical label when content exceeds one).

    ``data`` keys: store, address, order_ref, date, cashier, customer,
    items [{name, qty, price, total}], subtotal, tax, total, currency,
    footer, barcode.
    """
    d = _scaler(dpi)
    width = _dots(width_in, dpi)
    mx = d(18)
    fw = width - 2 * mx
    chars_per_line = max(8, int(fw / d(15)))   # ~15 dots/char at body font
    cur = data.get('currency') or '$'

    def money(v):
        try:
            return '%s%0.2f' % (cur, float(v))
        except (TypeError, ValueError):
            return '%s0.00' % cur

    rows = []
    y = 22

    def line(text, font, indent=0, just=None):
        nonlocal y
        if just:
            rows.append('^FO%d,%d^A0N,%d,%d^FB%d,1,0,%s^FD%s^FS'
                        % (mx, d(y), d(font), d(font), fw, just, text))
        else:
            rows.append('^FO%d,%d^A0N,%d,%d^FD%s^FS'
                        % (mx + indent, d(y), d(font), d(font), text))
        y += int(font * 1.25)

    def rule():
        nonlocal y
        rows.append('^FO%d,%d^GB%d,%d,%d^FS' % (mx, d(y), fw, d(2), d(2)))
        y += 12

    # Header
    line(_truncate(data.get('store'), 18), 40, just='C')
    if data.get('address'):
        rows.append('^FO%d,%d^A0N,%d,%d^FB%d,2,0,C^FD%s^FS'
                    % (mx, d(y), d(22), d(22), fw,
                       _truncate(data.get('address'), chars_per_line * 2)))
        y += int(22 * 1.25) * 2
    rule()

    line(_truncate(data.get('order_ref'), chars_per_line), 26)
    line(_sanitize(data.get('date')), 24)
    if data.get('cashier'):
        line('Cashier: %s' % _truncate(data.get('cashier'), chars_per_line - 9), 24)
    if data.get('customer'):
        line('Customer: %s' % _truncate(data.get('customer'), chars_per_line - 10), 24)
    rule()

    # Items
    for item in data.get('items', []):
        name = _sanitize(item.get('name'))
        nlines = 1 if len(name) <= chars_per_line else 2
        rows.append('^FO%d,%d^A0N,%d,%d^FB%d,2,0,L^FD%s^FS'
                    % (mx, d(y), d(26), d(26), fw,
                       _truncate(name, chars_per_line * 2)))
        y += int(26 * 1.25) * nlines
        left = '  %s x %s' % (item.get('qty'), money(item.get('price')))
        rows.append('^FO%d,%d^A0N,%d,%d^FD%s^FS'
                    % (mx, d(y), d(24), d(24), left))
        rows.append('^FO%d,%d^A0N,%d,%d^FB%d,1,0,R^FD%s^FS'
                    % (mx, d(y), d(24), d(24), fw, money(item.get('total'))))
        y += int(24 * 1.3)
    rule()

    def total_row(label, value, font):
        nonlocal y
        rows.append('^FO%d,%d^A0N,%d,%d^FD%s^FS'
                    % (mx, d(y), d(font), d(font), label))
        rows.append('^FO%d,%d^A0N,%d,%d^FB%d,1,0,R^FD%s^FS'
                    % (mx, d(y), d(font), d(font), fw, value))
        y += int(font * 1.35)

    total_row('Subtotal', money(data.get('subtotal')), 24)
    total_row('Tax', money(data.get('tax')), 24)
    total_row('TOTAL', money(data.get('total')), 38)
    y += 14

    footer = data.get('footer')
    if footer:
        for chunk in str(footer).splitlines():
            line(_truncate(chunk, chars_per_line), 22)

    barcode = _sanitize(data.get('barcode')) or _sanitize(data.get('order_ref'))
    if barcode:
        rows.append('^FO%d,%d^BY3^BCN,%d,Y,N,N^FD%s^FS'
                    % (mx, d(y), d(90), barcode))
        y += 90 + 24

    length = max(_dots(length_in, dpi), d(y + 20))
    zpl = _media_header(width, length, darkness, media)
    zpl.extend(rows)
    zpl.append('^XZ')
    return '\n'.join(zpl)
