from . import models


def post_init_hook(env):
    """Backfill mint_is_employee for every existing user.

    The field is computed+stored, but nothing has triggered its compute for rows
    that already exist at install time, so seed it once here.
    """
    users = env['res.users'].sudo().with_context(active_test=False).search([])
    users._mint_mark_is_employee_for_recompute()
    env.flush_all()
