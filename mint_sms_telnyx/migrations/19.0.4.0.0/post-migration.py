# -*- coding: utf-8 -*-
"""Map legacy single SMS consent to the transactional category only.

MR-1250 decision (Juan, 2026-08-11): consent captured before the
marketing/transactional split covers order/account texts only. Marketing
always requires a fresh explicit grant, so it is never set here.

Runs after the ORM added the sms_consent_* columns (post-migration).
Idempotent: only touches rows still lacking transactional consent.
"""


def migrate(cr, version):
    cr.execute(
        """
        UPDATE res_partner
           SET sms_consent_transactional = TRUE,
               sms_consent_transactional_date = sms_opt_in_date,
               sms_consent_transactional_source = sms_opt_in_source
         WHERE sms_opt_in = TRUE
           AND sms_consent_transactional IS DISTINCT FROM TRUE
        """
    )
