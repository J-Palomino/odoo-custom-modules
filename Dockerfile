# Custom Odoo 19 with Avancir Integration
FROM odoo:19

USER root

# Create extra-addons if it doesn't exist
RUN mkdir -p /mnt/extra-addons

# Copy custom addons - use ADD with checksum instead of COPY to invalidate cache
ADD avancir_inventory /mnt/extra-addons/avancir_inventory

# Set permissions and verify ir_cron.xml content
RUN chown -R odoo:odoo /mnt/extra-addons && \
    echo "=== ir_cron.xml content ===" && \
    cat /mnt/extra-addons/avancir_inventory/data/ir_cron.xml && \
    echo "=== Checking for numbercall ===" && \
    grep -c "numbercall" /mnt/extra-addons/avancir_inventory/data/ir_cron.xml || echo "numbercall NOT found (good!)"

USER odoo
