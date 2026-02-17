import logging

from . import models

_logger = logging.getLogger(__name__)


def _ensure_admin_all_companies(env):
    """Make sure admin (uid=2) is a member of every company."""
    admin = env['res.users'].browse(2)
    if not admin.exists():
        _logger.warning("Admin user (uid=2) not found, skipping company assignment")
        return

    all_companies = env['res.company'].search([])
    missing = all_companies - admin.company_ids
    if missing:
        admin.write({'company_ids': [(4, c.id) for c in missing]})
        _logger.info("Admin: added %d missing companies (%s)",
                     len(missing), missing.mapped('name'))
    else:
        _logger.info("Admin already has access to all %d companies", len(all_companies))


def _fix_orphaned_company_records(env):
    """Assign company_id to records where it is NULL.

    Uses raw SQL for bulk updates that can't be done efficiently via ORM.
    Idempotent — only updates records that still have company_id IS NULL.
    """
    cr = env.cr

    # Company partners: set company_id to their own company
    cr.execute("""
        UPDATE res_partner rp
        SET company_id = rc.id
        FROM res_company rc
        WHERE rc.partner_id = rp.id
          AND rp.company_id IS NULL
    """)
    if cr.rowcount:
        _logger.info("Fixed %d company partner records", cr.rowcount)

    # User-linked partners: set company_id to the user's company
    cr.execute("""
        UPDATE res_partner rp
        SET company_id = ru.company_id
        FROM res_users ru
        WHERE ru.partner_id = rp.id
          AND rp.company_id IS NULL
          AND ru.company_id IS NOT NULL
    """)
    if cr.rowcount:
        _logger.info("Fixed %d user-linked partner records", cr.rowcount)

    # Remaining partners: set to creator's company
    cr.execute("""
        UPDATE res_partner rp
        SET company_id = ru.company_id
        FROM res_users ru
        WHERE rp.create_uid = ru.id
          AND rp.company_id IS NULL
          AND rp.id NOT IN (1, 2, 3)
          AND ru.company_id IS NOT NULL
    """)
    if cr.rowcount:
        _logger.info("Fixed %d res.partner records with NULL company_id", cr.rowcount)

    # project.project: set company_id to creator's company
    cr.execute("""
        UPDATE project_project pp
        SET company_id = ru.company_id
        FROM res_users ru
        WHERE pp.create_uid = ru.id
          AND pp.company_id IS NULL
          AND ru.company_id IS NOT NULL
    """)
    if cr.rowcount:
        _logger.info("Fixed %d project.project records with NULL company_id", cr.rowcount)

    # project.task: inherit from parent project first, fallback to creator's company
    cr.execute("""
        UPDATE project_task pt
        SET company_id = pp.company_id
        FROM project_project pp
        WHERE pt.project_id = pp.id
          AND pt.company_id IS NULL
          AND pp.company_id IS NOT NULL
    """)
    if cr.rowcount:
        _logger.info("Fixed %d project.task records (from project) with NULL company_id", cr.rowcount)

    cr.execute("""
        UPDATE project_task pt
        SET company_id = ru.company_id
        FROM res_users ru
        WHERE pt.create_uid = ru.id
          AND pt.company_id IS NULL
          AND ru.company_id IS NOT NULL
    """)
    if cr.rowcount:
        _logger.info("Fixed %d project.task records (from creator) with NULL company_id", cr.rowcount)

    # slide_channel: set website_id if NULL (defaults to website 1)
    cr.execute("""
        UPDATE slide_channel
        SET website_id = 1
        WHERE website_id IS NULL
          AND EXISTS (SELECT 1 FROM website WHERE id = 1)
    """)
    if cr.rowcount:
        _logger.info("Fixed %d slide_channel records with NULL website_id", cr.rowcount)


def post_init_hook(env):
    """Post-install/upgrade hook: ensure admin can access all companies."""
    _logger.info("daisydo_multicompany post_init_hook starting...")
    _ensure_admin_all_companies(env)
    _fix_orphaned_company_records(env)
    _logger.info("daisydo_multicompany post_init_hook complete")
