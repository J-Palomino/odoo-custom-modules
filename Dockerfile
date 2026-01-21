# Odoo 19 with Avancir Module (no cron - v4)
FROM odoo:19
USER root
RUN mkdir -p /mnt/extra-addons
COPY --chown=odoo:odoo avancir_inventory /mnt/extra-addons/avancir_inventory
USER odoo
