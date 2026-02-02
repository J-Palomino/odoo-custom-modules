# Odoo 19 with Custom Modules
FROM odoo:19

# Bust all caches
ARG CACHEBUST=1
RUN echo "Build timestamp: $(date)" > /tmp/build-info.txt

USER root

# Install gosu for proper user switching in entrypoint
RUN apt-get update && apt-get install -y --no-install-recommends gosu && rm -rf /var/lib/apt/lists/*

# Prepare extra-addons directory
RUN mkdir -p /mnt/extra-addons && rm -rf /mnt/extra-addons/*

# Copy custom modules
COPY --chown=odoo:odoo avancir_inventory /mnt/extra-addons/avancir_inventory
COPY --chown=odoo:odoo mint_api_v2 /mnt/extra-addons/mint_api_v2

# Verify modules are present
RUN grep -q "identifier" /mnt/extra-addons/avancir_inventory/models/avancir_sync.py && echo "AVANCIR MODULE VERIFIED" || (echo "AVANCIR MODULE MISSING" && exit 1)
RUN test -f /mnt/extra-addons/mint_api_v2/__manifest__.py && echo "MINT_API_V2 MODULE VERIFIED" || (echo "MINT_API_V2 MODULE MISSING" && exit 1)

# Copy custom entrypoint that fixes volume permissions
COPY entrypoint.sh /entrypoint-custom.sh
RUN chmod +x /entrypoint-custom.sh

# Stay as root - entrypoint will switch to odoo after fixing permissions
ENTRYPOINT ["/entrypoint-custom.sh"]
CMD ["odoo"]
