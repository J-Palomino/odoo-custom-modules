# Odoo 19 with Custom Modules
FROM odoo:19

ARG CACHEBUST=27

USER root

# Prepare extra-addons directory
RUN mkdir -p /mnt/extra-addons && rm -rf /mnt/extra-addons/*

# Copy custom modules
COPY --chown=odoo:odoo avancir_inventory /mnt/extra-addons/avancir_inventory
COPY --chown=odoo:odoo mint_api_v2 /mnt/extra-addons/mint_api_v2
COPY --chown=odoo:odoo mint_theme /mnt/extra-addons/mint_theme

# OCA modules from submodules — temporarily disabled until Railway submodule
# support is verified. These modules exist locally via git submodules but the
# submodule contents are not always available in Railway's Docker build context.
# TODO: Re-enable after confirming Railway clones with --recurse-submodules
# COPY --chown=odoo:odoo oca/server-ux/date_range /mnt/extra-addons/date_range
# COPY --chown=odoo:odoo oca/reporting-engine/report_xlsx /mnt/extra-addons/report_xlsx
# COPY --chown=odoo:odoo oca/account-financial-reporting/account_financial_report /mnt/extra-addons/account_financial_report
# COPY --chown=odoo:odoo oca/account-financial-reporting/account_tax_balance /mnt/extra-addons/account_tax_balance
# COPY --chown=odoo:odoo oca/account-financial-reporting/partner_statement /mnt/extra-addons/partner_statement
# COPY --chown=odoo:odoo oca/account-reconcile/account_statement_base /mnt/extra-addons/account_statement_base
# COPY --chown=odoo:odoo oca/account-financial-tools/account_account_tag_code /mnt/extra-addons/account_account_tag_code
# COPY --chown=odoo:odoo oca/account-financial-tools/account_journal_restrict_mode /mnt/extra-addons/account_journal_restrict_mode
# COPY --chown=odoo:odoo oca/account-financial-tools/account_move_name_sequence /mnt/extra-addons/account_move_name_sequence
# COPY --chown=odoo:odoo oca/account-financial-tools/account_move_post_date_user /mnt/extra-addons/account_move_post_date_user
# COPY --chown=odoo:odoo oca/account-financial-tools/account_move_print /mnt/extra-addons/account_move_print
# COPY --chown=odoo:odoo oca/account-financial-tools/account_usability /mnt/extra-addons/account_usability
# COPY --chown=odoo:odoo oca/account-closing/account_invoice_start_end_dates /mnt/extra-addons/account_invoice_start_end_dates
# COPY --chown=odoo:odoo oca/account-analytic/account_analytic_tag /mnt/extra-addons/account_analytic_tag

# Directly tracked modules (not submodules)
COPY --chown=odoo:odoo account_financial_risk /mnt/extra-addons/account_financial_risk

# OCA Vault - End-to-end encrypted password vault
COPY --chown=odoo:odoo oca/server-auth/vault /mnt/extra-addons/vault

# Daisy Bot - AI assistant in Discuss
COPY --chown=odoo:odoo daisy_bot /mnt/extra-addons/daisy_bot

# Verify custom modules are present
RUN grep -q "identifier" /mnt/extra-addons/avancir_inventory/models/avancir_sync.py && echo "AVANCIR MODULE VERIFIED" || (echo "AVANCIR MODULE MISSING" && exit 1)
RUN test -f /mnt/extra-addons/mint_api_v2/__manifest__.py && echo "MINT_API_V2 MODULE VERIFIED" || (echo "MINT_API_V2 MODULE MISSING" && exit 1)
RUN test -f /mnt/extra-addons/mint_theme/__manifest__.py && echo "MINT_THEME MODULE VERIFIED" || (echo "MINT_THEME MODULE MISSING" && exit 1)
RUN cat /mnt/extra-addons/mint_theme/__manifest__.py | head -5 && echo "VERSION CHECK PASSED"
RUN ls -la /mnt/extra-addons/mint_theme/static/src/scss/ && echo "SCSS FILES CHECK PASSED"
RUN test -f /mnt/extra-addons/daisy_bot/__manifest__.py && echo "DAISY_BOT MODULE VERIFIED" || (echo "DAISY_BOT MODULE MISSING" && exit 1)
RUN test -f /mnt/extra-addons/vault/__manifest__.py && echo "VAULT MODULE VERIFIED" || (echo "VAULT MODULE MISSING" && exit 1)

# Verify OCA dependencies are present (submodule-based modules disabled for now)
# RUN test -f /mnt/extra-addons/date_range/__manifest__.py && echo "DATE_RANGE MODULE VERIFIED" || (echo "DATE_RANGE MODULE MISSING" && exit 1)
# RUN test -f /mnt/extra-addons/report_xlsx/__manifest__.py && echo "REPORT_XLSX MODULE VERIFIED" || (echo "REPORT_XLSX MODULE MISSING" && exit 1)
# RUN test -f /mnt/extra-addons/account_financial_report/__manifest__.py && echo "ACCOUNT_FINANCIAL_REPORT MODULE VERIFIED" || (echo "ACCOUNT_FINANCIAL_REPORT MODULE MISSING" && exit 1)

# Make theme generator executable
RUN chmod +x /mnt/extra-addons/mint_theme/generate-theme.sh

# Copy config file as backup and fix script
COPY odoo.conf /etc/odoo/odoo.conf
COPY fix-config.sh /fix-config.sh
RUN chmod +x /fix-config.sh

# Run as root — fix-config.sh handles user switch via /entrypoint.sh
ENTRYPOINT ["/fix-config.sh"]
CMD ["odoo"]
