# -*- coding: utf-8 -*-
"""19.0.5.0.0: Backfill Dutchie LocId/LspId on res.company, register the
publish-drain cron, verify config-params.

Why a post-migrate (not post_init_hook): the module is already installed
in prod, so a version bump triggers a migration but NOT post_init_hook.
The hook only fires on first install.

Mappings verified 2026-05-11 against `posv3/maintenance/GetLspLocationsBackend`
across all 5 accessible LSPs (575/576/805/820/723). FL has no Backoffice
access via the current credentials — fill in once a service account is
provisioned. Stores are joined on `dutchie_store_id` (UUID): unambiguous,
unlike names which can drift.
"""
import logging

_logger = logging.getLogger(__name__)

# State → LspId.
LSP_BY_STATE_CODE = {
    'AZ': 575,
    'MI': 576,
    'IL': 805,
    'NV': 820,
    'MO': 723,
    # 'FL': ???  — no Backoffice access yet
}

# dutchie_store_id (UUID) → LocId. Sourced from scripts/_resync-state-deals.mjs
# and packages/inventory-service/scripts/sync-all-markets.js in letsgomint-us.
LOC_ID_BY_UUID = {
    # Arizona (LSP 575)
    '92fd0875-cc39-479b-9fbd-0a7db947c694': 1568,  # Tempe
    '242ef5f9-5fc9-41ef-bf44-5a7a4a2954a9': 1571,  # Mesa
    '86441745-c533-4602-9264-f3a30db9753b': 2725,  # Scottsdale
    'f8dbdb94-7bea-4741-a3a9-6631d8430544': 2679,  # 75th Ave
    '1b81f825-ab2e-45c2-9ad7-add16236e95a': 2669,  # El Mirage
    'b5f4110e-6eb8-428b-aaaa-b4271d5eb327': 2551,  # Buckeye
    '1e30427d-d8db-4e60-84c8-8487d1d6bcd6': 2272,  # Northern Phoenix
    'a4d8b494-b542-4f69-acb8-cf67d6a6c3aa': 1570,  # Bell Road
    'fb72bed3-7489-43df-82e3-e93deb030a8c': 2668,  # East Mesa (Power Rd)
    # Michigan (LSP 576)
    '660d73ec-635f-437b-b064-a602623e4684': 1569,  # Kalamazoo
    '5c49a3be-6547-4dd0-975b-5644bc07d8c6': 2860,  # Roseville
    '80b86ddb-672e-4fcc-adf1-b6ebe7e9927f': 1574,  # Coldwater
    '62058ef65b73e100946b628d':              1867,  # Monroe
    '667d85c3a142f9f96981b4fd':              2859,  # New Buffalo
    'ff7a275b-251c-464a-93c2-de4b454cf265': 1868,  # Portage
    'b12241f5-d064-4100-bfe3-f8767250f7c3': 2897,  # Buchanan
    '0331909f-b320-4d88-b2ad-5b66844dc861': 2680,  # Mount Pleasant
    '0b65eac4-5a53-4c64-8144-41cfe3d588c6': 2736,  # Oxford
    # Nevada (LSP 820)
    'dbc5deb8-6bbc-4edc-b4df-11d7b2964de0': 2866,  # Las Vegas Strip / Paradise
    '577553d7-7604-4f34-948c-ee123dfd79ce': 2865,  # Spring Valley
    # Missouri (LSP 723)
    '6011bb98c3a8f600d16b9cc7':              2194,  # St. Peters
    # Illinois (LSP 805)
    'a3d2eecc-1703-4aae-bb52-3c6b133c0a9f': 2784,  # Willowbrook
}

PUSH_URL_KEY = 'mint.dutchie_discount_push.url'
PUSH_URL_DEFAULT = 'https://mintinvsvc-production-6aa5.up.railway.app/api/admin/discounts'
PUSH_MODE_KEY = 'mint.dutchie_discount_push.mode'


def _backfill_locids(cr):
    """Backfill dutchie_loc_id + dutchie_lsp_id on res.company. Idempotent —
    only writes empty fields, never overwrites existing values."""
    cr.execute("SELECT id, name, dutchie_store_id, state_id FROM res_company")
    rows = cr.fetchall()

    cr.execute(
        "SELECT id, code FROM res_country_state "
        "WHERE country_id = (SELECT id FROM res_country WHERE code='US')"
    )
    state_code_by_id = dict(cr.fetchall())

    backfilled = 0
    no_match = []
    for company_id, name, uuid, state_id in rows:
        loc_id = LOC_ID_BY_UUID.get(uuid)
        state_code = state_code_by_id.get(state_id)
        lsp_id = LSP_BY_STATE_CODE.get(state_code)
        if loc_id or lsp_id:
            cr.execute(
                "UPDATE res_company SET "
                "dutchie_loc_id = COALESCE(NULLIF(dutchie_loc_id, 0), %s), "
                "dutchie_lsp_id = COALESCE(NULLIF(dutchie_lsp_id, 0), %s) "
                "WHERE id = %s",
                (loc_id or 0, lsp_id or 0, company_id),
            )
            backfilled += cr.rowcount
        elif uuid:
            no_match.append((company_id, name, uuid))

    _logger.info('LocId/LspId backfill: %d companies updated', backfilled)
    if no_match:
        _logger.info(
            'LocId/LspId backfill: %d companies have dutchie_store_id but '
            'no mapping (likely FL or coming-soon): %s',
            len(no_match),
            ', '.join(f'{name} ({company_id})' for company_id, name, _ in no_match[:10]),
        )


def migrate(cr, version):
    """Migration entry point. Order:
      1. Backfill dutchie_loc_id / dutchie_lsp_id (SQL, no env needed)
      2. ORM-level setup (cron + config-param) — runs via the registry
    """
    _backfill_locids(cr)

    # The rest needs an ORM environment.
    from odoo import api, SUPERUSER_ID
    env = api.Environment(cr, SUPERUSER_ID, {})

    # ── Drain cron ────────────────────────────────────────────────────
    Cron = env['ir.cron'].sudo()
    discount_model = env['ir.model'].sudo().search(
        [('model', '=', 'mint.discount')], limit=1)
    cron_name = 'Mint: drain pending Dutchie discount publishes'
    if discount_model and not Cron.search([('name', '=', cron_name)], limit=1):
        Cron.create({
            'name': cron_name,
            'model_id': discount_model.id,
            'state': 'code',
            'code': 'model._cron_retry_dutchie_publish()',
            'interval_number': 1,
            'interval_type': 'minutes',
            'numbercall': -1,
            'active': True,
            'priority': 70,
        })
        _logger.info('Created %s cron (1-min interval)', cron_name)
    else:
        _logger.info('%s cron already exists or model missing', cron_name)

    # ── Config-param sanity ───────────────────────────────────────────
    Param = env['ir.config_parameter'].sudo()
    if not Param.get_param(PUSH_URL_KEY):
        Param.set_param(PUSH_URL_KEY, PUSH_URL_DEFAULT)
        _logger.info('Seeded %s = %s', PUSH_URL_KEY, PUSH_URL_DEFAULT)

    mode = Param.get_param(PUSH_MODE_KEY, 'off')
    _logger.info(
        '%s is currently %r. Step 2 ships dormant — flip to "dry-run" then '
        '"live" per market when ready (canary: MO → NV → IL → AZ).',
        PUSH_MODE_KEY, mode,
    )
