# Odoo 19 with Avancir Module (v6 - auth fix verified)
FROM odoo:19

# Bust all caches
ARG CACHEBUST=1
RUN echo "Build timestamp: $(date)" > /tmp/build-info.txt

USER root
RUN mkdir -p /mnt/extra-addons && rm -rf /mnt/extra-addons/*
COPY --chown=odoo:odoo avancir_inventory /mnt/extra-addons/avancir_inventory

# Verify the fix is present
RUN grep -q "identifier" /mnt/extra-addons/avancir_inventory/models/avancir_sync.py && echo "AUTH FIX VERIFIED" || (echo "AUTH FIX MISSING" && exit 1)

USER odoo
