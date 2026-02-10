# Odoo 19 with Custom Modules
FROM odoo:19

ARG CACHEBUST=25

USER root

# Prepare extra-addons directory
RUN mkdir -p /mnt/extra-addons && rm -rf /mnt/extra-addons/*

# Copy custom modules
COPY --chown=odoo:odoo avancir_inventory /mnt/extra-addons/avancir_inventory
COPY --chown=odoo:odoo mint_api_v2 /mnt/extra-addons/mint_api_v2
COPY --chown=odoo:odoo mint_theme /mnt/extra-addons/mint_theme
COPY --chown=odoo:odoo daisy_bot /mnt/extra-addons/daisy_bot

# OCA Dependencies (install first)
COPY --chown=odoo:odoo oca/server-ux/date_range /mnt/extra-addons/date_range
COPY --chown=odoo:odoo oca/reporting-engine/report_xlsx /mnt/extra-addons/report_xlsx

# OCA Priority 1: Financial Reporting
COPY --chown=odoo:odoo oca/account-financial-reporting/account_financial_report /mnt/extra-addons/account_financial_report
COPY --chown=odoo:odoo oca/account-financial-reporting/account_tax_balance /mnt/extra-addons/account_tax_balance
COPY --chown=odoo:odoo oca/account-financial-reporting/partner_statement /mnt/extra-addons/partner_statement

# OCA Priority 2: Bank Statement Import
COPY --chown=odoo:odoo oca/account-reconcile/account_statement_base /mnt/extra-addons/account_statement_base

# OCA Priority 3: Financial Tools
COPY --chown=odoo:odoo oca/account-financial-tools/account_account_tag_code /mnt/extra-addons/account_account_tag_code
COPY --chown=odoo:odoo oca/account-financial-tools/account_journal_restrict_mode /mnt/extra-addons/account_journal_restrict_mode
COPY --chown=odoo:odoo oca/account-financial-tools/account_move_name_sequence /mnt/extra-addons/account_move_name_sequence
COPY --chown=odoo:odoo oca/account-financial-tools/account_move_post_date_user /mnt/extra-addons/account_move_post_date_user
COPY --chown=odoo:odoo oca/account-financial-tools/account_move_print /mnt/extra-addons/account_move_print
COPY --chown=odoo:odoo oca/account-financial-tools/account_usability /mnt/extra-addons/account_usability

# OCA Priority 3: Account Closing, Analytic, Credit Control
COPY --chown=odoo:odoo oca/account-closing/account_invoice_start_end_dates /mnt/extra-addons/account_invoice_start_end_dates
COPY --chown=odoo:odoo oca/account-analytic/account_analytic_tag /mnt/extra-addons/account_analytic_tag
COPY --chown=odoo:odoo account_financial_risk /mnt/extra-addons/account_financial_risk

# Verify custom modules are present
RUN grep -q "identifier" /mnt/extra-addons/avancir_inventory/models/avancir_sync.py && echo "AVANCIR MODULE VERIFIED" || (echo "AVANCIR MODULE MISSING" && exit 1)
RUN test -f /mnt/extra-addons/mint_api_v2/__manifest__.py && echo "MINT_API_V2 MODULE VERIFIED" || (echo "MINT_API_V2 MODULE MISSING" && exit 1)
RUN test -f /mnt/extra-addons/mint_theme/__manifest__.py && echo "MINT_THEME MODULE VERIFIED" || (echo "MINT_THEME MODULE MISSING" && exit 1)
RUN grep "version" /mnt/extra-addons/mint_theme/__manifest__.py && echo "VERSION CHECK PASSED"
RUN test -f /mnt/extra-addons/daisy_bot/__manifest__.py && echo "DAISY_BOT MODULE VERIFIED" || (echo "DAISY_BOT MODULE MISSING" && exit 1)

# Verify OCA dependencies are present
RUN test -f /mnt/extra-addons/date_range/__manifest__.py && echo "DATE_RANGE MODULE VERIFIED" || (echo "DATE_RANGE MODULE MISSING" && exit 1)
RUN test -f /mnt/extra-addons/report_xlsx/__manifest__.py && echo "REPORT_XLSX MODULE VERIFIED" || (echo "REPORT_XLSX MODULE MISSING" && exit 1)
RUN test -f /mnt/extra-addons/account_financial_report/__manifest__.py && echo "ACCOUNT_FINANCIAL_REPORT MODULE VERIFIED" || (echo "ACCOUNT_FINANCIAL_REPORT MODULE MISSING" && exit 1)

# Make theme generator executable
RUN chmod +x /mnt/extra-addons/mint_theme/generate-theme.sh

# Copy config file as backup and fix script
COPY odoo.conf /etc/odoo/odoo.conf
COPY fix-config.sh /fix-config.sh
RUN chmod +x /fix-config.sh

USER odoo
ENTRYPOINT ["/fix-config.sh"]
CMD ["odoo"]
