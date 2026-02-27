from odoo import fields, models, tools


class MailMailReport(models.Model):
    _name = "mail.mail.report"
    _description = "Email Statistics"
    _auto = False
    _order = "date desc"

    date = fields.Date(string="Date", readonly=True)
    total_count = fields.Integer(string="Total", readonly=True)
    sent_count = fields.Integer(string="Sent", readonly=True)
    outgoing_count = fields.Integer(string="Outgoing", readonly=True)
    exception_count = fields.Integer(string="Failed", readonly=True)
    cancel_count = fields.Integer(string="Cancelled", readonly=True)

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute("""
            CREATE OR REPLACE VIEW %s AS (
                SELECT
                    row_number() OVER () AS id,
                    create_date::date AS date,
                    count(*) AS total_count,
                    count(*) FILTER (WHERE state = 'sent') AS sent_count,
                    count(*) FILTER (WHERE state = 'outgoing') AS outgoing_count,
                    count(*) FILTER (WHERE state = 'exception') AS exception_count,
                    count(*) FILTER (WHERE state = 'cancel') AS cancel_count
                FROM mail_mail
                GROUP BY create_date::date
            )
        """ % self._table)
