# Odoo 19 with Avancir Module (v5 - auth fix)
FROM odoo:19
USER root
RUN mkdir -p /mnt/extra-addons
# Force rebuild: 2026-01-22-v2
COPY --chown=odoo:odoo avancir_inventory /mnt/extra-addons/avancir_inventory
USER odoo
