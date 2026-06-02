# Odoo 19 with Custom Modules
FROM odoo:19

ARG CACHEBUST=128
# Force Docker to bust cache for all subsequent layers when CACHEBUST changes
# Touch timestamp forces layer invalidation even if BuildKit thinks nothing changed
RUN echo "Build cache key: $CACHEBUST — $(date +%s)"

USER root

# Install git (needed for OCA module cloning), nginx (websocket reverse proxy), and cloudflared (tunnel)
RUN apt-get update \
    && apt-get install -y --no-install-recommends git nginx curl \
    && curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o /tmp/cloudflared.deb \
    && dpkg -i /tmp/cloudflared.deb \
    && rm -f /tmp/cloudflared.deb \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /run/nginx /etc/cloudflared

# Install Python dependencies for base_accounting_kit + push notifications + S3 storage
RUN pip3 install --no-cache-dir --break-system-packages --ignore-installed openpyxl ofxparse qifparse pywebpush "fsspec[s3]>=2025.3.0" packaging PyJWT redis pynacl cssselect

# Prepare extra-addons directory
RUN mkdir -p /opt/extra-addons && rm -rf /opt/extra-addons/*

# Remove stale module copies from default addons path (prevents Odoo
# from loading old versions instead of our fresh copies in extra-addons)
RUN rm -rf /var/lib/odoo/addons/19.0/mint_*

# ── OCA storage modules (S3 attachments) ──────────────────────────────
# Patched locally: server_environment dependency removed from fs_storage
# (its monkeypatching of add_to_registry breaks Odoo 19 model registration)
COPY --chown=odoo:odoo fs_storage /opt/extra-addons/fs_storage
COPY --chown=odoo:odoo fs_attachment /opt/extra-addons/fs_attachment
COPY --chown=odoo:odoo fs_attachment_s3 /opt/extra-addons/fs_attachment_s3

# ── Mint custom modules (CACHEBUST=106) ─────────────────────────────
COPY --chown=odoo:odoo avancir_inventory /opt/extra-addons/avancir_inventory
COPY --chown=odoo:odoo mint_api_v2 /opt/extra-addons/mint_api_v2
COPY --chown=odoo:odoo mint_theme /opt/extra-addons/mint_theme
COPY --chown=odoo:odoo mint_maintenance_form /opt/extra-addons/mint_maintenance_form
COPY --chown=odoo:odoo account_financial_risk /opt/extra-addons/account_financial_risk
COPY --chown=odoo:odoo purchase_price_precision /opt/extra-addons/purchase_price_precision
COPY --chown=odoo:odoo mint_push /opt/extra-addons/mint_push
COPY --chown=odoo:odoo mint_command_center /opt/extra-addons/mint_command_center
COPY --chown=odoo:odoo mint_flipbook /opt/extra-addons/mint_flipbook
COPY --chown=odoo:odoo mint_dutchie_discount_mirror /opt/extra-addons/mint_dutchie_discount_mirror
COPY --chown=odoo:odoo mint_redis_push /opt/extra-addons/mint_redis_push
COPY --chown=odoo:odoo mint_banner /opt/extra-addons/mint_banner
COPY --chown=odoo:odoo mint_visual_cms /opt/extra-addons/mint_visual_cms
COPY --chown=odoo:odoo mint_recruitment_portal /opt/extra-addons/mint_recruitment_portal
COPY --chown=odoo:odoo mint_hr_complaints /opt/extra-addons/mint_hr_complaints

# ── Patch: neuter the "page is out of date" watcher (bus module) ────
# The module-level patch in mint_command_center only suppresses the notification
# display, but the original service races ahead during initialization. This
# replaces the entire service file so the watcher never starts.
COPY --chown=odoo:odoo patches/outdated_page_watcher_service.js \
     /usr/lib/python3/dist-packages/odoo/addons/bus/static/src/outdated_page_watcher_service.js

COPY --chown=odoo:odoo mint_embed /opt/extra-addons/mint_embed
COPY --chown=odoo:odoo mint_oauth_only /opt/extra-addons/mint_oauth_only
COPY --chown=odoo:odoo mint_customer_api /opt/extra-addons/mint_customer_api
COPY --chown=odoo:odoo mint_dutchie_sync /opt/extra-addons/mint_dutchie_sync
COPY --chown=odoo:odoo mint_pos_bridge /opt/extra-addons/mint_pos_bridge
COPY --chown=odoo:odoo mint_redis_session /opt/extra-addons/mint_redis_session
COPY --chown=odoo:odoo mint_inventory_ops /opt/extra-addons/mint_inventory_ops
COPY --chown=odoo:odoo mint_mail_whitelist /opt/extra-addons/mint_mail_whitelist
COPY --chown=odoo:odoo mint_posthog /opt/extra-addons/mint_posthog
COPY --chown=odoo:odoo mint_sw_buster /opt/extra-addons/mint_sw_buster
COPY --chown=odoo:odoo mint_loki_logger /opt/extra-addons/mint_loki_logger
COPY --chown=odoo:odoo mint_sms_telnyx /opt/extra-addons/mint_sms_telnyx
COPY --chown=odoo:odoo mint_messaging_menu_jump /opt/extra-addons/mint_messaging_menu_jump

# ── DaisyDo modules ─────────────────────────────────────────────────
COPY --chown=odoo:odoo daisy_bot /opt/extra-addons/daisy_bot
COPY --chown=odoo:odoo daisy_error_handler /opt/extra-addons/daisy_error_handler
COPY --chown=odoo:odoo daisydo_theme /opt/extra-addons/daisydo_theme
COPY --chown=odoo:odoo daisydo_livechat /opt/extra-addons/daisydo_livechat
COPY --chown=odoo:odoo daisydo_agents /opt/extra-addons/daisydo_agents
COPY --chown=odoo:odoo mint_agent_provisioner /opt/extra-addons/mint_agent_provisioner
COPY --chown=odoo:odoo daisydo_multicompany /opt/extra-addons/daisydo_multicompany
COPY --chown=odoo:odoo daisydo_webhook /opt/extra-addons/daisydo_webhook

# ── Cybrosys accounting modules ─────────────────────────────────────
COPY --chown=odoo:odoo base_accounting_kit /opt/extra-addons/base_accounting_kit
COPY --chown=odoo:odoo base_account_budget /opt/extra-addons/base_account_budget

# ── OCA DMS modules (Document Management System) ─────────────────────
COPY --chown=odoo:odoo dms /opt/extra-addons/dms
COPY --chown=odoo:odoo dms_field /opt/extra-addons/dms_field
COPY --chown=odoo:odoo hr_dms_field /opt/extra-addons/hr_dms_field

# ── ONLYOFFICE connector (edit attachments via Document Server) ──────
COPY --chown=odoo:odoo onlyoffice_odoo /opt/extra-addons/onlyoffice_odoo

# ── Mail FK hardening (idempotent FK repairs for module installs) ────
COPY --chown=odoo:odoo mint_mail_fk_hardening /opt/extra-addons/mint_mail_fk_hardening

# ── DROPPED 2026-05-20: OCA Tier Validation / Knowledge / QMS bundle ──
# 13 OCA modules (base_tier_validation*, document_knowledge, document_page*,
# mgmtsystem*) were ported from 18.0→19.0 in commit 3946172 but shipped
# multiple v19-incompat bugs:
#   • base_tier_validation: top-level `await import` in systray JS →
#     entire web.assets_backend bundle failed, white-paged the SPA
#   • mgmtsystem_nonconformity: stale _thread_to_store override on
#     mail.thread → first TypeError on signature, then RecursionError on
#     store.add (Odoo 19 dispatches recursively via Store.add → _thread_to_store)
# Module files still live in the repo (odoo-custom-modules/<module>/) for
# future reference, but they are NOT shipped to the image. Re-installing
# requires a proper v19 port + staging smoke test. See memory entry
# `oca-port-bundle-v19-broken` for the full incident log.

# ── OCA modules (flattened from submodules) ──────────────────────────
COPY --chown=odoo:odoo vault /opt/extra-addons/vault
# sign_oca removed — not Odoo 19 compatible, leaving orphaned DB refs
COPY --chown=odoo:odoo base_cancel_confirm /opt/extra-addons/base_cancel_confirm
COPY --chown=odoo:odoo base_substate /opt/extra-addons/base_substate
COPY --chown=odoo:odoo base_technical_features /opt/extra-addons/base_technical_features
COPY --chown=odoo:odoo date_range /opt/extra-addons/date_range
COPY --chown=odoo:odoo bi_sql_editor /opt/extra-addons/bi_sql_editor
COPY --chown=odoo:odoo report_qweb_element_page_visibility /opt/extra-addons/report_qweb_element_page_visibility
COPY --chown=odoo:odoo report_xlsx /opt/extra-addons/report_xlsx
COPY --chown=odoo:odoo report_xlsx_helper /opt/extra-addons/report_xlsx_helper
COPY --chown=odoo:odoo report_xml /opt/extra-addons/report_xml
COPY --chown=odoo:odoo sql_request_abstract /opt/extra-addons/sql_request_abstract
COPY --chown=odoo:odoo spreadsheet_oca /opt/extra-addons/spreadsheet_oca
COPY --chown=odoo:odoo spreadsheet_dashboard_oca /opt/extra-addons/spreadsheet_dashboard_oca
COPY --chown=odoo:odoo account_analytic_tag /opt/extra-addons/account_analytic_tag
COPY --chown=odoo:odoo account_invoice_start_end_dates /opt/extra-addons/account_invoice_start_end_dates
COPY --chown=odoo:odoo account_financial_report /opt/extra-addons/account_financial_report
COPY --chown=odoo:odoo account_tax_balance /opt/extra-addons/account_tax_balance
COPY --chown=odoo:odoo partner_statement /opt/extra-addons/partner_statement
COPY --chown=odoo:odoo account_account_tag_code /opt/extra-addons/account_account_tag_code
COPY --chown=odoo:odoo account_journal_restrict_mode /opt/extra-addons/account_journal_restrict_mode
COPY --chown=odoo:odoo account_move_name_sequence /opt/extra-addons/account_move_name_sequence
COPY --chown=odoo:odoo account_move_post_date_user /opt/extra-addons/account_move_post_date_user
COPY --chown=odoo:odoo account_move_print /opt/extra-addons/account_move_print
COPY --chown=odoo:odoo account_usability /opt/extra-addons/account_usability
COPY --chown=odoo:odoo account_invoice_fixed_discount /opt/extra-addons/account_invoice_fixed_discount
COPY --chown=odoo:odoo account_invoice_pricelist /opt/extra-addons/account_invoice_pricelist
COPY --chown=odoo:odoo account_invoice_pricelist_sale /opt/extra-addons/account_invoice_pricelist_sale
COPY --chown=odoo:odoo account_statement_base /opt/extra-addons/account_statement_base

# ── OCA appointments stack (resource_booking + slot-duration helper) ──
COPY --chown=odoo:odoo web_calendar_slot_duration /opt/extra-addons/web_calendar_slot_duration
COPY --chown=odoo:odoo resource_booking /opt/extra-addons/resource_booking

# ── Verify critical modules ─────────────────────────────────────────
RUN grep -q "identifier" /opt/extra-addons/avancir_inventory/models/avancir_sync.py && echo "AVANCIR MODULE VERIFIED" || (echo "AVANCIR MODULE MISSING" && exit 1)
RUN test -f /opt/extra-addons/mint_api_v2/__manifest__.py && echo "MINT_API_V2 MODULE VERIFIED" || (echo "MINT_API_V2 MODULE MISSING" && exit 1)
RUN test -f /opt/extra-addons/mint_theme/__manifest__.py && echo "MINT_THEME MODULE VERIFIED" || (echo "MINT_THEME MODULE MISSING" && exit 1)
RUN grep "version" /opt/extra-addons/mint_theme/__manifest__.py && echo "VERSION CHECK PASSED"
RUN test -f /opt/extra-addons/mint_maintenance_form/__manifest__.py && echo "MINT_MAINTENANCE_FORM MODULE VERIFIED" || (echo "MINT_MAINTENANCE_FORM MODULE MISSING" && exit 1)
RUN grep "version" /opt/extra-addons/mint_push/__manifest__.py && echo "MINT_PUSH MODULE VERIFIED" || (echo "MINT_PUSH MODULE MISSING" && exit 1)
RUN grep "version" /opt/extra-addons/mint_command_center/__manifest__.py && echo "MINT_COMMAND_CENTER MODULE VERIFIED" || (echo "MINT_COMMAND_CENTER MODULE MISSING" && exit 1)
RUN grep "push_subscription_views" /opt/extra-addons/mint_push/__manifest__.py | head -1 && echo "LOAD ORDER CHECK OK"
RUN test -f /opt/extra-addons/mint_banner/__manifest__.py && echo "MINT_BANNER MODULE VERIFIED" || (echo "MINT_BANNER MODULE MISSING" && exit 1)
RUN test -f /opt/extra-addons/mint_visual_cms/__manifest__.py && echo "MINT_VISUAL_CMS MODULE VERIFIED" || (echo "MINT_VISUAL_CMS MODULE MISSING" && exit 1)
RUN test -f /opt/extra-addons/mint_embed/__manifest__.py && echo "MINT_EMBED MODULE VERIFIED" || (echo "MINT_EMBED MODULE MISSING" && exit 1)
RUN test -f /opt/extra-addons/mint_customer_api/__manifest__.py && echo "MINT_CUSTOMER_API VERIFIED" || (echo "MINT_CUSTOMER_API MISSING" && exit 1)
RUN test -f /opt/extra-addons/mint_dutchie_sync/__manifest__.py && echo "MINT_DUTCHIE_SYNC VERIFIED" || (echo "MINT_DUTCHIE_SYNC MISSING" && exit 1)
RUN test -f /opt/extra-addons/mint_pos_bridge/__manifest__.py && echo "MINT_POS_BRIDGE VERIFIED" || (echo "MINT_POS_BRIDGE MISSING" && exit 1)
RUN test -f /opt/extra-addons/mint_redis_session/__manifest__.py && echo "MINT_REDIS_SESSION VERIFIED" || (echo "MINT_REDIS_SESSION MISSING" && exit 1)
RUN test -f /opt/extra-addons/mint_inventory_ops/__manifest__.py && echo "MINT_INVENTORY_OPS VERIFIED" || (echo "MINT_INVENTORY_OPS MISSING" && exit 1)
RUN test -f /opt/extra-addons/mint_sms_telnyx/__manifest__.py && echo "MINT_SMS_TELNYX VERIFIED" || (echo "MINT_SMS_TELNYX MISSING" && exit 1)
RUN python3 -c "compile(open('/opt/extra-addons/mint_sms_telnyx/models/sms_sms.py').read(), 'sms_sms.py', 'exec')" && echo "SMS_SMS SYNTAX OK" || (echo "SMS_SMS SYNTAX ERROR" && head -60 /opt/extra-addons/mint_sms_telnyx/models/sms_sms.py && exit 1)
RUN python3 -c "compile(open('/opt/extra-addons/mint_pos_bridge/models/pos_order.py').read(), 'pos_order.py', 'exec')" && echo "POS_ORDER SYNTAX OK" || (echo "POS_ORDER SYNTAX ERROR" && head -60 /opt/extra-addons/mint_pos_bridge/models/pos_order.py && exit 1)
RUN test -f /opt/extra-addons/daisy_bot/__manifest__.py && echo "DAISY_BOT MODULE VERIFIED" || (echo "DAISY_BOT MODULE MISSING" && exit 1)
RUN test -f /opt/extra-addons/daisy_error_handler/__manifest__.py && echo "DAISY_ERROR_HANDLER MODULE VERIFIED" || (echo "DAISY_ERROR_HANDLER MODULE MISSING" && exit 1)
RUN test -f /opt/extra-addons/vault/__manifest__.py && echo "VAULT MODULE VERIFIED" || (echo "VAULT MODULE MISSING" && exit 1)

# Verify mint_posthog module
RUN test -f /opt/extra-addons/mint_posthog/__manifest__.py && echo "MINT_POSTHOG MODULE VERIFIED" || (echo "MINT_POSTHOG MODULE MISSING" && exit 1)
RUN python3 -c "import ast; ast.literal_eval(open('/opt/extra-addons/mint_posthog/__manifest__.py').read())" && echo "MINT_POSTHOG MANIFEST PARSE OK" || (echo "MINT_POSTHOG MANIFEST PARSE FAILED" && exit 1)
RUN test -f /opt/extra-addons/mint_posthog/static/src/posthog_boot.js && echo "MINT_POSTHOG JS ASSET VERIFIED" || (echo "MINT_POSTHOG JS ASSET MISSING" && exit 1)

# Verify DaisyDo modules
RUN for mod in daisydo_theme daisydo_livechat daisydo_agents daisydo_multicompany daisydo_webhook; do \
      test -f /opt/extra-addons/$mod/__manifest__.py && echo "$mod VERIFIED" || (echo "$mod MISSING" && exit 1); \
    done

# Verify Cybrosys modules
RUN for mod in base_accounting_kit base_account_budget; do \
      test -f /opt/extra-addons/$mod/__manifest__.py && echo "$mod VERIFIED" || (echo "$mod MISSING" && exit 1); \
    done

# Verify OCA modules
RUN for mod in spreadsheet_oca spreadsheet_dashboard_oca \
      base_cancel_confirm base_substate base_technical_features date_range \
      bi_sql_editor report_qweb_element_page_visibility report_xlsx report_xlsx_helper report_xml sql_request_abstract \
      account_analytic_tag account_invoice_start_end_dates \
      account_financial_report account_tax_balance partner_statement \
      account_account_tag_code account_journal_restrict_mode account_move_name_sequence \
      account_move_post_date_user account_move_print account_usability \
      account_invoice_fixed_discount account_invoice_pricelist account_invoice_pricelist_sale \
      account_statement_base account_financial_risk; do \
      test -f /opt/extra-addons/$mod/__manifest__.py && echo "$mod VERIFIED" || (echo "$mod MISSING" && exit 1); \
    done

# OCA Tier Validation / Knowledge / QMS bundle verification — REMOVED 2026-05-27.
# These 13 modules were intentionally dropped from the image on 2026-05-20
# (see the "DROPPED" comment block at the top of this Dockerfile). The verify
# block that previously lived here was orphaned by that change and caused
# every PR preview build to fail with "$mod MISSING".

# Verify OCA storage modules
RUN for mod in fs_storage fs_attachment fs_attachment_s3; do \
      test -f /opt/extra-addons/$mod/__manifest__.py && echo "$mod VERIFIED" || (echo "$mod MISSING" && exit 1); \
    done

# Verify OCA appointments modules
RUN for mod in web_calendar_slot_duration resource_booking; do \
      test -f /opt/extra-addons/$mod/__manifest__.py && echo "$mod VERIFIED" || (echo "$mod MISSING" && exit 1); \
    done

# Verify OCA DMS modules
RUN for mod in dms dms_field hr_dms_field; do \
      test -f /opt/extra-addons/$mod/__manifest__.py && echo "$mod VERIFIED" || (echo "$mod MISSING" && exit 1); \
    done

# Make theme generators executable
RUN chmod +x /opt/extra-addons/mint_theme/generate-theme.sh
RUN chmod +x /opt/extra-addons/daisydo_theme/generate-theme.sh

# Copy config file as backup, nginx template, and fix script
COPY odoo.conf /etc/odoo/odoo.conf
COPY nginx.conf.template /etc/nginx/nginx.conf.template
COPY fix-config.sh /fix-config.sh
RUN chmod +x /fix-config.sh

# ── Cloudflare Tunnel config ──────────────────────────────────────────
COPY cloudflared-config.yml /etc/cloudflared/config.yml
COPY start-tunnel.sh /start-tunnel.sh
RUN chmod +x /start-tunnel.sh

# Expose nginx port (Railway routes traffic here)
EXPOSE 8080

# Run as root — fix-config.sh handles user switch via /entrypoint.sh
ENTRYPOINT ["/fix-config.sh"]
CMD ["odoo"]
