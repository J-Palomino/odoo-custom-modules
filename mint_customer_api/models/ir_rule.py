# -*- coding: utf-8 -*-
import re

from odoo import _, models

# The stock Odoo access-error hint (odoo/addons/base/models/ir_rule.py) ends
# with a joke telling the blocked user to bribe their admin with "a batch of
# freshly baked cookies". Match that whole sentence so we can swap it for
# something a real user can act on. DOTALL because the source wraps the line.
_COOKIE_HINT_RE = re.compile(
    r"If you really, really need access,.*?freshly baked cookies\.",
    re.DOTALL,
)


class IrRule(models.Model):
    _inherit = "ir.rule"

    def _make_access_error(self, operation, records):
        """Replace the jokey 'freshly baked cookies' resolution hint with an
        actionable one, picked by inspecting the rules that actually failed:

        * A company-scoped rule (e.g. "Task: multi-company isolation") — the
          record exists and the user may read it, just not under the companies
          active in this session. Stock Odoo only suggests switching company
          when the record HAS a company (`_get_redirect_suggested_company`);
          a company-less record (company_id=False, admitted only while the
          parent company is active) gets no hint at all. Tell the user exactly
          which company to tick and where the switcher lives.
        * Anything else — keep the data-privacy wording: what an internal user
          sees when a record references a restricted customer contact
          (partner_rule_hide_web_customers).

        We override the factory rather than reimplement it: take the exception
        Odoo builds, rewrite just the hint sentence, return it.
        """
        exception = super()._make_access_error(operation, records)
        try:
            message = exception.args[0]
        except (IndexError, TypeError):
            return exception
        if not isinstance(message, str) or "freshly baked cookies" not in message:
            return exception

        replacement = _(
            "You don't have permission to view this record. It may reference a "
            "contact restricted by our data-privacy rules. If you need access, "
            "please contact your system administrator."
        )

        # Same company detection stock Odoo uses when building the message:
        # did any of the failing rules filter on company_id?
        try:
            failing = self._get_failing(records, mode=operation).sudo()
            company_related = any(
                "company_id" in (rule.domain_force or "") for rule in failing
            )
        except Exception:
            company_related = False

        if company_related:
            company = None
            try:
                rec = records[:1].sudo()
                if "company_id" in rec._fields and rec.company_id:
                    # The record belongs to one company: that company must be
                    # among the session's active ones.
                    company = rec.company_id
                else:
                    # Company-less record: the isolation rule admits it only
                    # while the parent company (id=1, "Mint Cannabis") is
                    # active — mirror that so the hint names the company that
                    # actually unlocks the record.
                    parent = self.env["res.company"].sudo().browse(1)
                    company = parent if parent.exists() else None
            except Exception:
                company = None
            if company:
                replacement = _(
                    "This record is only visible when the right company is "
                    'active. Open the company selector in the TOP-RIGHT corner '
                    'of Odoo, tick "%s" (your other companies can stay '
                    "selected), and reload the page. If it still fails, "
                    "contact your system administrator.",
                    company.display_name,
                )
            else:
                replacement = _(
                    "This record is only visible when the right company is "
                    "active. Open the company selector in the TOP-RIGHT corner "
                    "of Odoo, enable the company this record belongs to, and "
                    "reload the page. If it still fails, contact your system "
                    "administrator."
                )

        # re.sub treats backslashes in the replacement as escapes — a company
        # display name containing one would corrupt the message.
        exception.args = (
            _COOKIE_HINT_RE.sub(replacement.replace("\\", "\\\\"), message),
            *exception.args[1:],
        )
        return exception
