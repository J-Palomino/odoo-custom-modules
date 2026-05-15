"""
Pre-migration for 19.0.3.1.0 — remap brand_id from res.partner to mint.brand.

The Many2one target for brand_id on mint.ptl.deal, mint.deal.submission,
mint.hotbox.deal, and mint.brand.calendar.entry changed from `res.partner`
to `mint.brand` in this release. The underlying DB column stays integer,
but the IDs it references must be remapped to point to matching
`mint.brand` records (by name).

Runs as raw SQL so it can execute before the ORM reloads with the new
model definitions.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    _logger.info("mint_command_center: remapping brand_id (res.partner -> mint.brand)")

    # Build normalized name → mint.brand.id map
    cr.execute("SELECT id, name FROM mint_brand")
    brand_rows = cr.fetchall()

    def norm(s):
        return "".join(c for c in (s or "").lower() if c.isalnum())

    brand_by_norm = {norm(name): bid for bid, name in brand_rows if name}
    _logger.info("  %d mint.brand records loaded", len(brand_by_norm))

    # --- 1. Single Many2one fields: brand_id on 4 tables ---
    tables = [
        "mint_ptl_deal",
        "mint_deal_submission",
        "mint_hotbox_deal",
        "mint_brand_calendar_entry",
    ]
    total_remapped = 0
    total_orphaned = 0

    for table in tables:
        # Skip tables that don't exist yet (fresh install)
        cr.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
            (table,),
        )
        if not cr.fetchone():
            _logger.info("  %s: table not found, skipping", table)
            continue

        cr.execute(
            f"SELECT DISTINCT brand_id FROM {table} WHERE brand_id IS NOT NULL"
        )
        partner_ids = [r[0] for r in cr.fetchall()]
        if not partner_ids:
            _logger.info("  %s: no brand_id values to remap", table)
            continue

        # Get partner names
        cr.execute(
            "SELECT id, name FROM res_partner WHERE id IN %s", (tuple(partner_ids),)
        )
        partner_to_name = {pid: pname for pid, pname in cr.fetchall()}

        remapped = 0
        orphaned = 0
        for partner_id in partner_ids:
            partner_name = partner_to_name.get(partner_id)
            if not partner_name:
                # Partner was deleted or inaccessible — NULL out the stale reference
                cr.execute(
                    f"UPDATE {table} SET brand_id = NULL WHERE brand_id = %s",
                    (partner_id,),
                )
                orphaned += cr.rowcount
                continue

            brand_id = brand_by_norm.get(norm(partner_name))
            if brand_id:
                cr.execute(
                    f"UPDATE {table} SET brand_id = %s WHERE brand_id = %s",
                    (brand_id, partner_id),
                )
                remapped += cr.rowcount
            else:
                # No matching mint.brand — NULL so the record stays valid
                cr.execute(
                    f"UPDATE {table} SET brand_id = NULL WHERE brand_id = %s",
                    (partner_id,),
                )
                orphaned += cr.rowcount
                _logger.info(
                    "    %s: no mint.brand match for partner '%s' (id=%s) — nulled",
                    table,
                    partner_name,
                    partner_id,
                )

        _logger.info("  %s: %d rows remapped, %d nulled (no match)", table, remapped, orphaned)
        total_remapped += remapped
        total_orphaned += orphaned

    # --- 2. Many2many exclude table on mint.ptl.deal ---
    # The relation table has columns (deal_id, partner_id). The new field
    # definition uses the same relation table but with column name `brand_id`.
    # We need to:
    #   a) Remap partner_id values → mint.brand.id
    #   b) Rename the column partner_id → brand_id
    cr.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = 'mint_ptl_deal_excluded_brand_rel'"
    )
    if cr.fetchone():
        # Check if the old column still exists
        cr.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'mint_ptl_deal_excluded_brand_rel'
            """
        )
        cols = {r[0] for r in cr.fetchall()}

        if "partner_id" in cols and "brand_id" not in cols:
            # Remap partner_id values to brand IDs by name match
            cr.execute(
                """
                SELECT DISTINCT r.partner_id, p.name
                FROM mint_ptl_deal_excluded_brand_rel r
                JOIN res_partner p ON p.id = r.partner_id
                """
            )
            partner_name_pairs = cr.fetchall()

            # Delete rows whose partner has no name match, then remap the rest
            to_delete = []
            to_update = []
            for partner_id, partner_name in partner_name_pairs:
                brand_id = brand_by_norm.get(norm(partner_name))
                if brand_id:
                    to_update.append((partner_id, brand_id))
                else:
                    to_delete.append(partner_id)

            deleted = 0
            if to_delete:
                cr.execute(
                    "DELETE FROM mint_ptl_deal_excluded_brand_rel WHERE partner_id IN %s",
                    (tuple(to_delete),),
                )
                deleted = cr.rowcount

            updated = 0
            for partner_id, brand_id in to_update:
                cr.execute(
                    "UPDATE mint_ptl_deal_excluded_brand_rel SET partner_id = %s WHERE partner_id = %s",
                    (brand_id, partner_id),
                )
                updated += cr.rowcount

            # Rename column so the new model definition sees `brand_id`
            cr.execute(
                "ALTER TABLE mint_ptl_deal_excluded_brand_rel RENAME COLUMN partner_id TO brand_id"
            )

            # Drop old FK to res_partner (Odoo will recreate the FK to mint_brand)
            cr.execute(
                """
                SELECT conname FROM pg_constraint
                WHERE conrelid = 'mint_ptl_deal_excluded_brand_rel'::regclass
                  AND contype = 'f'
                """
            )
            for (conname,) in cr.fetchall():
                cr.execute(f"ALTER TABLE mint_ptl_deal_excluded_brand_rel DROP CONSTRAINT {conname}")

            _logger.info(
                "  mint_ptl_deal_excluded_brand_rel: %d rows remapped, %d deleted, column renamed",
                updated,
                deleted,
            )

    _logger.info(
        "mint_command_center migration complete: %d brand_id values remapped, %d nulled",
        total_remapped,
        total_orphaned,
    )
