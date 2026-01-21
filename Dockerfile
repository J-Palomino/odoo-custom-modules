# Custom Odoo 19 with Avancir Integration
FROM odoo:19

USER root

# Create extra-addons if it doesn't exist
RUN mkdir -p /mnt/extra-addons

# Copy custom addons
COPY avancir_inventory /mnt/extra-addons/avancir_inventory

# Set permissions and verify
RUN chown -R odoo:odoo /mnt/extra-addons && \
    echo "=== Contents of /mnt/extra-addons ===" && \
    ls -la /mnt/extra-addons/ && \
    echo "=== Contents of avancir_inventory ===" && \
    ls -la /mnt/extra-addons/avancir_inventory/

USER odoo
