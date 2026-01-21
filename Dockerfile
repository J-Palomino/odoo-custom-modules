# Custom Odoo 19 with Avancir Integration
# Build ID: 20260121-v3
FROM odoo:19 AS base

USER root

# Create addon directory and ensure fresh build
RUN mkdir -p /mnt/extra-addons && rm -rf /mnt/extra-addons/*

# Copy module - v3 with fixed ir_cron.xml (no numbercall, no doall)
COPY --chown=odoo:odoo avancir_inventory /mnt/extra-addons/avancir_inventory

# Verify the file content
RUN echo "=== Verifying ir_cron.xml ===" && \
    cat /mnt/extra-addons/avancir_inventory/data/ir_cron.xml && \
    echo "=== Should NOT contain numbercall or doall ===" && \
    ! grep -q "numbercall\|doall" /mnt/extra-addons/avancir_inventory/data/ir_cron.xml && \
    echo "Verification PASSED!"

USER odoo
