# Custom Odoo 19 with Avancir Integration
FROM odoo:19

USER root

# Create addon directory
RUN mkdir -p /mnt/extra-addons

# Copy module (timestamps will bust cache)
COPY avancir_inventory /mnt/extra-addons/avancir_inventory

# Fix permissions
RUN chown -R odoo:odoo /mnt/extra-addons

USER odoo
