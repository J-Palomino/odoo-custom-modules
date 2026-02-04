# Odoo 19 with Custom Modules
FROM odoo:19

ARG CACHEBUST=7

USER root

# Install git for cloning OCA modules
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

# Prepare extra-addons directory
RUN mkdir -p /mnt/extra-addons && rm -rf /mnt/extra-addons/*

# Clone OCA Knowledge modules (wiki/document pages)
RUN git clone --depth 1 --branch 19.0 https://github.com/OCA/knowledge.git /tmp/oca-knowledge && \
    cp -r /tmp/oca-knowledge/document_page /mnt/extra-addons/ && \
    cp -r /tmp/oca-knowledge/document_page_approval /mnt/extra-addons/ && \
    cp -r /tmp/oca-knowledge/document_page_tag /mnt/extra-addons/ && \
    cp -r /tmp/oca-knowledge/document_page_project /mnt/extra-addons/ && \
    cp -r /tmp/oca-knowledge/document_knowledge /mnt/extra-addons/ && \
    cp -r /tmp/oca-knowledge/document_page_access_group /mnt/extra-addons/ && \
    rm -rf /tmp/oca-knowledge && \
    chown -R odoo:odoo /mnt/extra-addons/document_*

# Copy custom modules
COPY --chown=odoo:odoo avancir_inventory /mnt/extra-addons/avancir_inventory
COPY --chown=odoo:odoo mint_api_v2 /mnt/extra-addons/mint_api_v2

# Verify modules are present
RUN grep -q "identifier" /mnt/extra-addons/avancir_inventory/models/avancir_sync.py && echo "AVANCIR MODULE VERIFIED" || (echo "AVANCIR MODULE MISSING" && exit 1)
RUN test -f /mnt/extra-addons/mint_api_v2/__manifest__.py && echo "MINT_API_V2 MODULE VERIFIED" || (echo "MINT_API_V2 MODULE MISSING" && exit 1)

# Copy config file as backup and fix script
COPY odoo.conf /etc/odoo/odoo.conf
COPY fix-config.sh /fix-config.sh
RUN chmod +x /fix-config.sh

USER odoo
ENTRYPOINT ["/fix-config.sh"]
CMD ["odoo"]
