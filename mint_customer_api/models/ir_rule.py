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
        actionable one.

        This is the tail of every record-rule AccessError, including the
        customer-isolation rule (partner_rule_hide_web_customers) — i.e. what
        an internal user sees when a record references a restricted customer
        contact. We override the factory rather than reimplement it: take the
        exception Odoo builds, rewrite just the hint sentence, return it.
        """
        exception = super()._make_access_error(operation, records)
        replacement = _(
            "You don't have permission to view this record. It may reference a "
            "contact restricted by our data-privacy rules. If you need access, "
            "please contact your system administrator."
        )
        try:
            message = exception.args[0]
            if "freshly baked cookies" in message:
                exception.args = (
                    _COOKIE_HINT_RE.sub(replacement, message),
                    *exception.args[1:],
                )
        except (IndexError, TypeError):
            pass
        return exception
