# Odoo 19 with Custom Modules
FROM odoo:19

ARG CACHEBUST=22

USER root

# Prepare extra-addons directory
RUN mkdir -p /mnt/extra-addons && rm -rf /mnt/extra-addons/*

# Copy custom modules
COPY --chown=odoo:odoo avancir_inventory /mnt/extra-addons/avancir_inventory
COPY --chown=odoo:odoo mint_api_v2 /mnt/extra-addons/mint_api_v2
COPY --chown=odoo:odoo mint_theme /mnt/extra-addons/mint_theme

# Verify modules are present
RUN grep -q "identifier" /mnt/extra-addons/avancir_inventory/models/avancir_sync.py && echo "AVANCIR MODULE VERIFIED" || (echo "AVANCIR MODULE MISSING" && exit 1)
RUN test -f /mnt/extra-addons/mint_api_v2/__manifest__.py && echo "MINT_API_V2 MODULE VERIFIED" || (echo "MINT_API_V2 MODULE MISSING" && exit 1)
RUN test -f /mnt/extra-addons/mint_theme/__manifest__.py && echo "MINT_THEME MODULE VERIFIED" || (echo "MINT_THEME MODULE MISSING" && exit 1)
RUN grep "version" /mnt/extra-addons/mint_theme/__manifest__.py && echo "VERSION CHECK PASSED"

# Make theme generator executable
RUN chmod +x /mnt/extra-addons/mint_theme/generate-theme.sh

# Copy config file as backup and fix script
COPY odoo.conf /etc/odoo/odoo.conf
COPY fix-config.sh /fix-config.sh
RUN chmod +x /fix-config.sh

USER odoo
ENTRYPOINT ["/fix-config.sh"]
CMD ["odoo"]
