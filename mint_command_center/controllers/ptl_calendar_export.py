import csv
import io
import logging

from odoo import http
from odoo.http import request, content_disposition

_logger = logging.getLogger(__name__)


CSV_HEADERS = [
    'Date',
    'Market',
    'Day State',
    'Deal',
    'Brand',
    'Category',
    'Sale Type',
    'Discount Type',
    'Discount Value',
    'Original Price',
    'Display Text',
    'Deal State',
    'Featured',
    'Sequence',
]


class PtlCalendarExportController(http.Controller):

    @http.route(
        '/mint/ptl-calendar/export.csv',
        type='http',
        auth='user',
        methods=['GET'],
        csrf=False,
    )
    def export_csv(self, ids='', **kwargs):
        # `ids` is a comma-separated list of mint.ptl.day record ids.
        try:
            id_list = [int(x) for x in (ids or '').split(',') if x.strip()]
        except ValueError:
            return request.not_found()
        if not id_list:
            return request.not_found()

        Day = request.env['mint.ptl.day']
        days = Day.browse(id_list).exists()
        # Respect record rules — `exists()` already drops missing/forbidden ids
        # since browse() returns an empty recordset slot for unreadable records.
        if not days:
            return request.not_found()

        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator='\n')
        writer.writerow(CSV_HEADERS)

        for day in days.sorted(lambda d: (d.date or False, d.market_id.name or '')):
            deals = (
                day.deal_ids.sorted(lambda d: (d.sequence, d.id))
                if day.deal_ids
                else day.deal_ids
            )
            day_state = dict(day._fields['state'].selection).get(day.state, day.state or '')
            market = day.market_id.name or ''
            date_str = day.date.isoformat() if day.date else ''

            if not deals:
                writer.writerow([date_str, market, day_state] + [''] * (len(CSV_HEADERS) - 3))
                continue

            for deal in deals:
                writer.writerow([
                    date_str,
                    market,
                    day_state,
                    deal.name or '',
                    deal.brand_id.name or '',
                    deal.product_category or '',
                    dict(deal._fields['sale_type'].selection).get(deal.sale_type, '') if deal.sale_type else '',
                    dict(deal._fields['discount_type'].selection).get(deal.discount_type, '') if deal.discount_type else '',
                    deal.discount_value or 0.0,
                    deal.original_price or 0.0,
                    (deal.sales_details or '').replace('\n', ' '),
                    dict(deal._fields['state'].selection).get(deal.state, '') if deal.state else '',
                    'Yes' if deal.is_featured else 'No',
                    deal.sequence or 0,
                ])

        body = buf.getvalue().encode('utf-8-sig')
        filename = 'ptl_calendar.csv'
        return request.make_response(
            body,
            headers=[
                ('Content-Type', 'text/csv; charset=utf-8'),
                ('Content-Disposition', content_disposition(filename)),
                ('Content-Length', str(len(body))),
            ],
        )
