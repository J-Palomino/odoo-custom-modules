"""Keep MintDeals customers out of the activity "Assigned to" picker.

``maintenance.request.user_id`` (Technician) already carries a
``share = False`` domain, but the *activity* assignee escaped it. Odoo renders
``mail.activity.user_id`` — labelled "Assigned to" — through
``mail.mail_activity_view_form_popup`` as a bare
``<field name="user_id" widget="many2one_avatar_user"/>`` with no domain, and no
view in the database (calendar and resource_booking only touch ``invisible``)
adds one. Its dropdown is therefore an unfiltered ``res.users`` name_search: 876
MintDeals portal customers against 315 internal staff, so roughly three of every
four rows offered when scheduling an activity on a ticket is a customer name.

``mint_customer_api``'s ``users_rule_hide_web_customers`` hides those rows from
regular internal users, but it is ORed away for anyone in Mint Web Customer
Access — which ``base.group_erp_manager`` implies, so all 16 admins see the full
list. That rule answers "may this person look up customer PII at all"; it is the
wrong instrument for this, because the answer here does not depend on the
viewer. A share user cannot open the backend and therefore can never action an
activity, so a customer is never a valid assignee for anyone. Restricting the
field itself states that once, for every viewer, and leaves the PII-access
question to the record rules where it belongs.

Field-level rather than view-level so the guard also covers the chatter's
"Assign to", the activity systray and any future view that renders the field.
Domains constrain the UI only, so the single pre-existing activity assigned to a
portal account keeps working and is not rewritten by this change.
"""
from odoo import fields, models


class MailActivity(models.Model):
    _inherit = "mail.activity"

    user_id = fields.Many2one(domain="[('share', '=', False)]")
