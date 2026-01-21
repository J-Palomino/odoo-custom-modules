# Custom Odoo 19 with Avancir Integration
FROM odoo:19

USER root

# Copy custom addons
COPY avancir_inventory /mnt/extra-addons/avancir_inventory

# Set permissions
RUN chown -R odoo:odoo /mnt/extra-addons

USER odoo
