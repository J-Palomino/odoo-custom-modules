/**
 * Visual CMS — Wireframe Interaction Layer
 *
 * Handles: zone click → side panel, banner AJAX loading, drag reorder,
 * store selector navigation, cache clear (Publish Now).
 */
(function () {
    'use strict';

    // ── State ───────────────────────────────────────────────────────
    let activeSlot = null;
    let activeZoneEl = null;
    let panelOpen = false;

    // ── DOM refs (set on init) ──────────────────────────────────────
    let panel, panelTitle, panelBody, panelClose, publishBtn, storeSelector;

    // ── Helpers ─────────────────────────────────────────────────────

    function jsonRpc(url, params) {
        return fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ jsonrpc: '2.0', method: 'call', id: 1, params: params }),
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (data.error) throw new Error(data.error.message || 'RPC error');
            return data.result;
        });
    }

    function toast(message, type) {
        type = type || 'info';
        var el = document.createElement('div');
        el.className = 'vcms-toast vcms-toast--' + type;
        el.textContent = message;
        document.body.appendChild(el);
        setTimeout(function () { el.remove(); }, 3500);
    }

    function getOdooFormUrl(bannerId) {
        return '/odoo/mint.banner/' + bannerId;
    }

    function getOdooNewUrl(slot, companyId) {
        var url = '/odoo/mint.banner/new';
        var params = [];
        if (slot) params.push('default_slot=' + encodeURIComponent(slot));
        if (companyId) params.push('default_company_id=' + companyId);
        if (params.length) url += '?' + params.join('&');
        return url;
    }

    // ── Panel rendering ─────────────────────────────────────────────

    function openPanel() {
        if (!panelOpen) {
            panel.classList.add('vcms-panel--open');
            panelOpen = true;
        }
    }

    function closePanel() {
        panel.classList.remove('vcms-panel--open');
        panelOpen = false;
        if (activeZoneEl) {
            activeZoneEl.classList.remove('vcms-zone--active');
            activeZoneEl = null;
        }
        activeSlot = null;
    }

    function renderPanelLoading(slotLabel) {
        panelTitle.textContent = slotLabel;
        panelBody.innerHTML = '<div class="vcms-loading">Loading banners</div>';
        // Remove existing actions footer
        var existing = panel.querySelector('.vcms-panel__actions');
        if (existing) existing.remove();
    }

    function renderPanelBanners(slotLabel, banners, slot, companyId) {
        panelTitle.textContent = slotLabel + ' (' + banners.length + ')';

        var html = '';

        if (banners.length === 0) {
            html += '<p class="text-muted">No banners in this slot yet.</p>';
        }

        for (var i = 0; i < banners.length; i++) {
            var b = banners[i];
            var statusClass = b.status === 'published' ? 'bg-success'
                : b.status === 'scheduled' ? 'bg-warning text-dark'
                : b.status === 'expired' ? 'bg-danger'
                : 'bg-secondary';

            html += '<div class="vcms-banner-card" data-banner-id="' + b.id + '">';
            html += '  <span class="vcms-banner-card__drag" title="Drag to reorder">&#9776;</span>';

            if (b.image_url) {
                html += '  <img class="vcms-banner-card__thumb" src="' + escapeHtml(b.image_url) + '" alt="" loading="lazy"/>';
            } else {
                html += '  <div class="vcms-banner-card__thumb--empty">&#128247;</div>';
            }

            html += '  <div class="vcms-banner-card__info">';
            html += '    <div class="vcms-banner-card__name" title="' + escapeHtml(b.name) + '">' + escapeHtml(b.name) + '</div>';
            html += '    <div class="vcms-banner-card__detail">';
            html += '      <span class="badge ' + statusClass + '" style="font-size:0.65rem">' + escapeHtml(b.status) + '</span>';
            html += '      <span class="ms-1">' + escapeHtml(b.company_name) + '</span>';
            if (b.date_start || b.date_end) {
                html += '    <br/><small>' + escapeHtml(b.date_start || '...') + ' — ' + escapeHtml(b.date_end || '...') + '</small>';
            }
            html += '    </div>';
            html += '  </div>';

            html += '  <div class="vcms-banner-card__actions">';
            html += '    <a href="' + getOdooFormUrl(b.id) + '" target="_blank" class="btn btn-sm btn-outline-secondary" title="Edit in Odoo">&#9998;</a>';
            html += '  </div>';
            html += '</div>';
        }

        panelBody.innerHTML = html;

        // Actions footer
        var existing = panel.querySelector('.vcms-panel__actions');
        if (existing) existing.remove();

        var footer = document.createElement('div');
        footer.className = 'vcms-panel__actions';
        footer.innerHTML =
            '<a href="' + getOdooNewUrl(slot, companyId) + '" target="_blank" class="btn btn-primary btn-sm flex-grow-1">' +
            '+ Add Banner</a>' +
            '<button class="btn btn-outline-secondary btn-sm vcms-refresh-btn" title="Refresh">&#8635;</button>';
        panel.appendChild(footer);

        // Refresh button handler
        footer.querySelector('.vcms-refresh-btn').addEventListener('click', function () {
            loadSlotBanners(slot, companyId, slotLabel);
        });

        // Drag reorder
        initDragReorder();
    }

    function escapeHtml(str) {
        if (!str) return '';
        var div = document.createElement('div');
        div.appendChild(document.createTextNode(str));
        return div.innerHTML;
    }

    // ── AJAX ────────────────────────────────────────────────────────

    function loadSlotBanners(slot, companyId, slotLabel) {
        renderPanelLoading(slotLabel);
        openPanel();

        jsonRpc('/visual-cms/api/slot-banners', {
            slot: slot,
            company_id: companyId || false,
        })
        .then(function (banners) {
            renderPanelBanners(slotLabel, banners, slot, companyId);
        })
        .catch(function (err) {
            panelBody.innerHTML = '<p class="text-danger">Failed to load banners: ' + escapeHtml(err.message) + '</p>';
        });
    }

    function clearCache() {
        publishBtn.disabled = true;
        publishBtn.textContent = 'Publishing...';

        jsonRpc('/visual-cms/api/clear-cache', {})
        .then(function (result) {
            toast('Frontend cache cleared — changes live in ~2 min', 'success');
        })
        .catch(function (err) {
            toast('Cache clear failed: ' + err.message, 'error');
        })
        .finally(function () {
            publishBtn.disabled = false;
            publishBtn.textContent = 'Publish Now';
        });
    }

    function reorderBanners(bannerIds) {
        jsonRpc('/visual-cms/api/reorder', { banner_ids: bannerIds })
        .then(function () {
            toast('Banner order saved', 'success');
        })
        .catch(function (err) {
            toast('Reorder failed: ' + err.message, 'error');
        });
    }

    // ── Drag reorder ────────────────────────────────────────────────

    function initDragReorder() {
        var cards = panelBody.querySelectorAll('.vcms-banner-card');
        var dragSrc = null;

        cards.forEach(function (card) {
            var handle = card.querySelector('.vcms-banner-card__drag');
            if (!handle) return;

            card.setAttribute('draggable', 'true');

            card.addEventListener('dragstart', function (e) {
                dragSrc = card;
                card.style.opacity = '0.4';
                e.dataTransfer.effectAllowed = 'move';
            });

            card.addEventListener('dragover', function (e) {
                e.preventDefault();
                e.dataTransfer.dropEffect = 'move';
                card.style.borderTop = '2px solid #0d6efd';
            });

            card.addEventListener('dragleave', function () {
                card.style.borderTop = '';
            });

            card.addEventListener('drop', function (e) {
                e.preventDefault();
                card.style.borderTop = '';
                if (dragSrc && dragSrc !== card) {
                    panelBody.insertBefore(dragSrc, card);
                    // Collect new order
                    var ids = [];
                    panelBody.querySelectorAll('.vcms-banner-card').forEach(function (c) {
                        ids.push(parseInt(c.getAttribute('data-banner-id')));
                    });
                    reorderBanners(ids);
                }
            });

            card.addEventListener('dragend', function () {
                card.style.opacity = '';
                dragSrc = null;
            });
        });
    }

    // ── Init ────────────────────────────────────────────────────────

    function init() {
        panel = document.getElementById('vcmsPanel');
        panelTitle = document.getElementById('vcmsPanelTitle');
        panelBody = document.getElementById('vcmsPanelBody');
        panelClose = document.getElementById('vcmsPanelClose');
        publishBtn = document.getElementById('vcmsPublishBtn');
        storeSelector = document.getElementById('vcmsStoreSelector');

        if (!panel) return; // Not on wireframe page

        // Zone click handlers
        document.querySelectorAll('.vcms-zone--editable').forEach(function (zone) {
            zone.addEventListener('click', function () {
                var slot = zone.getAttribute('data-slot');
                var companyId = zone.getAttribute('data-company-id');
                var label = zone.querySelector('.vcms-zone__label').textContent.trim();

                if (!slot) return;

                // Deselect previous
                if (activeZoneEl) activeZoneEl.classList.remove('vcms-zone--active');

                // Select this
                zone.classList.add('vcms-zone--active');
                activeZoneEl = zone;
                activeSlot = slot;

                loadSlotBanners(slot, companyId, label);
            });
        });

        // Close panel
        if (panelClose) {
            panelClose.addEventListener('click', closePanel);
        }

        // Publish button
        if (publishBtn) {
            publishBtn.addEventListener('click', clearCache);
        }

        // Store selector navigation
        if (storeSelector) {
            storeSelector.addEventListener('change', function () {
                var slug = storeSelector.value;
                if (slug) {
                    window.location.href = '/visual-cms/' + slug;
                }
            });
        }
    }

    // Run on DOMContentLoaded and Odoo website page loads
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
