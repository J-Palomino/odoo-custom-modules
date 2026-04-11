from . import purchase_order
from . import purchase_order_line
# bill_of_lading disabled 2026-04-11: tables were never created on production
# (schema sync didn't run cleanly during prior upgrades), and keeping the
# classes in the Python registry spams `registry.check_tables_exist` on every
# boot. Re-enable when the tables are properly created via a clean upgrade.
# from . import bill_of_lading
