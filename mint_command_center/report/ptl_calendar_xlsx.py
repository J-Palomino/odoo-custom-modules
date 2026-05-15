from odoo import models


class PtlCalendarXlsx(models.AbstractModel):
    _name = 'report.mint_command_center.report_ptl_calendar_xlsx'
    _description = 'PTL Calendar Excel Report'
    _inherit = 'report.report_xlsx.abstract'

    COLUMNS = [
        ('Date', 14),
        ('Market', 18),
        ('Day State', 12),
        ('Deal', 32),
        ('Brand', 22),
        ('Category', 16),
        ('Sale Type', 14),
        ('Discount Type', 16),
        ('Discount Value', 14),
        ('Original Price', 14),
        ('Display Text', 40),
        ('Deal State', 12),
        ('Featured', 10),
        ('Sequence', 10),
    ]

    def generate_xlsx_report(self, workbook, data, docs):
        bold = workbook.add_format({'bold': True, 'bg_color': '#D9E1F2', 'border': 1})
        date_fmt = workbook.add_format({'num_format': 'yyyy-mm-dd'})
        money_fmt = workbook.add_format({'num_format': '$#,##0.00'})
        wrap_fmt = workbook.add_format({'text_wrap': True, 'valign': 'top'})

        sheet = workbook.add_worksheet('PTL Calendar')

        # Header
        for col_idx, (header, width) in enumerate(self.COLUMNS):
            sheet.set_column(col_idx, col_idx, width)
            sheet.write(0, col_idx, header, bold)
        sheet.freeze_panes(1, 0)
        sheet.autofilter(0, 0, 0, len(self.COLUMNS) - 1)

        row = 1
        for day in docs.sorted(lambda d: (d.date or False, d.market_id.name or '')):
            deals = day.deal_ids.sorted(lambda d: (d.sequence, d.id)) if day.deal_ids else day.deal_ids
            if not deals:
                # Still emit a row for the empty day so it shows in the export
                sheet.write_datetime(row, 0, day.date, date_fmt) if day.date else sheet.write(row, 0, '')
                sheet.write(row, 1, day.market_id.name or '')
                sheet.write(row, 2, dict(day._fields['state'].selection).get(day.state, day.state or ''))
                for col in range(3, len(self.COLUMNS)):
                    sheet.write(row, col, '')
                row += 1
                continue

            for deal in deals:
                if day.date:
                    sheet.write_datetime(row, 0, day.date, date_fmt)
                else:
                    sheet.write(row, 0, '')
                sheet.write(row, 1, day.market_id.name or '')
                sheet.write(row, 2, dict(day._fields['state'].selection).get(day.state, day.state or ''))
                sheet.write(row, 3, deal.name or '')
                sheet.write(row, 4, deal.brand_id.name or '')
                sheet.write(row, 5, deal.product_category or '')
                sheet.write(
                    row, 6,
                    dict(deal._fields['sale_type'].selection).get(deal.sale_type, '') if deal.sale_type else '',
                )
                sheet.write(
                    row, 7,
                    dict(deal._fields['discount_type'].selection).get(deal.discount_type, '') if deal.discount_type else '',
                )
                sheet.write_number(row, 8, deal.discount_value or 0.0)
                sheet.write_number(row, 9, deal.original_price or 0.0, money_fmt)
                sheet.write(row, 10, deal.sales_details or '', wrap_fmt)
                sheet.write(
                    row, 11,
                    dict(deal._fields['state'].selection).get(deal.state, '') if deal.state else '',
                )
                sheet.write(row, 12, 'Yes' if deal.is_featured else 'No')
                sheet.write_number(row, 13, deal.sequence or 0)
                row += 1
