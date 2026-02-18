# Odoo 19 with Custom Modules
FROM odoo:19

ARG CACHEBUST=30

USER root

# Install Python dependencies for base_accounting_kit
RUN pip3 install --no-cache-dir openpyxl ofxparse qifparse

# Prepare extra-addons directory
RUN mkdir -p /mnt/extra-addons && rm -rf /mnt/extra-addons/*

# ── Mint custom modules ──────────────────────────────────────────────
COPY --chown=odoo:odoo avancir_inventory /mnt/extra-addons/avancir_inventory
COPY --chown=odoo:odoo mint_api_v2 /mnt/extra-addons/mint_api_v2
COPY --chown=odoo:odoo mint_theme /mnt/extra-addons/mint_theme
COPY --chown=odoo:odoo account_financial_risk /mnt/extra-addons/account_financial_risk

# ── DaisyDo modules ─────────────────────────────────────────────────
COPY --chown=odoo:odoo daisy_bot /mnt/extra-addons/daisy_bot
COPY --chown=odoo:odoo daisydo_theme /mnt/extra-addons/daisydo_theme
COPY --chown=odoo:odoo daisydo_livechat /mnt/extra-addons/daisydo_livechat
COPY --chown=odoo:odoo daisydo_agents /mnt/extra-addons/daisydo_agents
COPY --chown=odoo:odoo daisydo_multicompany /mnt/extra-addons/daisydo_multicompany
COPY --chown=odoo:odoo daisydo_webhook /mnt/extra-addons/daisydo_webhook

# ── Cybrosys accounting modules ─────────────────────────────────────
COPY --chown=odoo:odoo base_accounting_kit /mnt/extra-addons/base_accounting_kit
COPY --chown=odoo:odoo base_account_budget /mnt/extra-addons/base_account_budget

# ── OCA: server-auth ────────────────────────────────────────────────
COPY --chown=odoo:odoo oca/server-auth/vault /mnt/extra-addons/vault

# ── OCA: sign (18.0 port — already installed on instance) ───────────
COPY --chown=odoo:odoo oca/sign/sign_oca /mnt/extra-addons/sign_oca

# ── OCA: server-ux ──────────────────────────────────────────────────
COPY --chown=odoo:odoo oca/server-ux/base_cancel_confirm /mnt/extra-addons/base_cancel_confirm
COPY --chown=odoo:odoo oca/server-ux/base_substate /mnt/extra-addons/base_substate
COPY --chown=odoo:odoo oca/server-ux/base_technical_features /mnt/extra-addons/base_technical_features
COPY --chown=odoo:odoo oca/server-ux/date_range /mnt/extra-addons/date_range

# ── OCA: reporting-engine ───────────────────────────────────────────
COPY --chown=odoo:odoo oca/reporting-engine/bi_sql_editor /mnt/extra-addons/bi_sql_editor
COPY --chown=odoo:odoo oca/reporting-engine/report_qweb_element_page_visibility /mnt/extra-addons/report_qweb_element_page_visibility
COPY --chown=odoo:odoo oca/reporting-engine/report_xlsx /mnt/extra-addons/report_xlsx
COPY --chown=odoo:odoo oca/reporting-engine/report_xlsx_helper /mnt/extra-addons/report_xlsx_helper
COPY --chown=odoo:odoo oca/reporting-engine/report_xml /mnt/extra-addons/report_xml
COPY --chown=odoo:odoo oca/reporting-engine/sql_request_abstract /mnt/extra-addons/sql_request_abstract

# ── OCA: spreadsheet (18.0 port) ──────────────────────────────────
COPY --chown=odoo:odoo oca/spreadsheet/spreadsheet_oca /mnt/extra-addons/spreadsheet_oca
COPY --chown=odoo:odoo oca/spreadsheet/spreadsheet_dashboard_oca /mnt/extra-addons/spreadsheet_dashboard_oca

# ── OCA: account-analytic ───────────────────────────────────────────
COPY --chown=odoo:odoo oca/account-analytic/account_analytic_tag /mnt/extra-addons/account_analytic_tag

# ── OCA: account-closing ────────────────────────────────────────────
COPY --chown=odoo:odoo oca/account-closing/account_invoice_start_end_dates /mnt/extra-addons/account_invoice_start_end_dates

# ── OCA: account-financial-reporting ────────────────────────────────
COPY --chown=odoo:odoo oca/account-financial-reporting/account_financial_report /mnt/extra-addons/account_financial_report
COPY --chown=odoo:odoo oca/account-financial-reporting/account_tax_balance /mnt/extra-addons/account_tax_balance
COPY --chown=odoo:odoo oca/account-financial-reporting/partner_statement /mnt/extra-addons/partner_statement

# ── OCA: account-financial-tools ────────────────────────────────────
COPY --chown=odoo:odoo oca/account-financial-tools/account_account_tag_code /mnt/extra-addons/account_account_tag_code
COPY --chown=odoo:odoo oca/account-financial-tools/account_journal_restrict_mode /mnt/extra-addons/account_journal_restrict_mode
COPY --chown=odoo:odoo oca/account-financial-tools/account_move_name_sequence /mnt/extra-addons/account_move_name_sequence
COPY --chown=odoo:odoo oca/account-financial-tools/account_move_post_date_user /mnt/extra-addons/account_move_post_date_user
COPY --chown=odoo:odoo oca/account-financial-tools/account_move_print /mnt/extra-addons/account_move_print
COPY --chown=odoo:odoo oca/account-financial-tools/account_usability /mnt/extra-addons/account_usability

# ── OCA: account-invoicing ──────────────────────────────────────────
COPY --chown=odoo:odoo oca/account-invoicing/account_invoice_fixed_discount /mnt/extra-addons/account_invoice_fixed_discount
COPY --chown=odoo:odoo oca/account-invoicing/account_invoice_pricelist /mnt/extra-addons/account_invoice_pricelist
COPY --chown=odoo:odoo oca/account-invoicing/account_invoice_pricelist_sale /mnt/extra-addons/account_invoice_pricelist_sale

# ── OCA: account-reconcile ──────────────────────────────────────────
COPY --chown=odoo:odoo oca/account-reconcile/account_statement_base /mnt/extra-addons/account_statement_base

# ── Verify critical modules ─────────────────────────────────────────
RUN grep -q "identifier" /mnt/extra-addons/avancir_inventory/models/avancir_sync.py && echo "AVANCIR MODULE VERIFIED" || (echo "AVANCIR MODULE MISSING" && exit 1)
RUN test -f /mnt/extra-addons/mint_api_v2/__manifest__.py && echo "MINT_API_V2 MODULE VERIFIED" || (echo "MINT_API_V2 MODULE MISSING" && exit 1)
RUN test -f /mnt/extra-addons/mint_theme/__manifest__.py && echo "MINT_THEME MODULE VERIFIED" || (echo "MINT_THEME MODULE MISSING" && exit 1)
RUN grep "version" /mnt/extra-addons/mint_theme/__manifest__.py && echo "VERSION CHECK PASSED"
RUN test -f /mnt/extra-addons/daisy_bot/__manifest__.py && echo "DAISY_BOT MODULE VERIFIED" || (echo "DAISY_BOT MODULE MISSING" && exit 1)
RUN test -f /mnt/extra-addons/vault/__manifest__.py && echo "VAULT MODULE VERIFIED" || (echo "VAULT MODULE MISSING" && exit 1)

# Verify DaisyDo modules
RUN for mod in daisydo_theme daisydo_livechat daisydo_agents daisydo_multicompany daisydo_webhook; do \
      test -f /mnt/extra-addons/$mod/__manifest__.py && echo "$mod VERIFIED" || (echo "$mod MISSING" && exit 1); \
    done

# Verify Cybrosys modules
RUN for mod in base_accounting_kit base_account_budget; do \
      test -f /mnt/extra-addons/$mod/__manifest__.py && echo "$mod VERIFIED" || (echo "$mod MISSING" && exit 1); \
    done

# Verify OCA modules
RUN for mod in sign_oca spreadsheet_oca spreadsheet_dashboard_oca \
      base_cancel_confirm base_substate base_technical_features date_range \
      bi_sql_editor report_qweb_element_page_visibility report_xlsx report_xlsx_helper report_xml sql_request_abstract \
      account_analytic_tag account_invoice_start_end_dates \
      account_financial_report account_tax_balance partner_statement \
      account_account_tag_code account_journal_restrict_mode account_move_name_sequence \
      account_move_post_date_user account_move_print account_usability \
      account_invoice_fixed_discount account_invoice_pricelist account_invoice_pricelist_sale \
      account_statement_base account_financial_risk; do \
      test -f /mnt/extra-addons/$mod/__manifest__.py && echo "$mod VERIFIED" || (echo "$mod MISSING" && exit 1); \
    done

# Make theme generator executable
RUN chmod +x /mnt/extra-addons/mint_theme/generate-theme.sh

# Copy config file as backup and fix script
COPY odoo.conf /etc/odoo/odoo.conf
COPY fix-config.sh /fix-config.sh
RUN chmod +x /fix-config.sh

# Run as root — fix-config.sh handles user switch via /entrypoint.sh
ENTRYPOINT ["/fix-config.sh"]
CMD ["odoo"]
