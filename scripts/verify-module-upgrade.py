#!/usr/bin/env python3
"""Post-upgrade verification: did the modules we tried to update actually update?

## Why this exists

`entrypoint.sh` runs `odoo -u ... --stop-after-init` and, on failure, prints
"Module update failed, but continuing startup..." and starts the server anyway.
That is deliberate — a failed upgrade should not take the site down — but it
means a rolled-back upgrade is indistinguishable from a successful one from the
outside:

  * Railway reports the deploy SUCCESS (the container started)
  * /web/login returns 200 (Odoo serves fine on the OLD module versions)
  * `fields_get` even reports NEW fields, because Odoo creates missing columns
    during registry setup regardless of `-u` — so the module looks *installed*
    while none of its views, menus or data registered

Observed 2026-08-28: one invalid view in mint_api_v2 aborted the shared `-u`
batch and left FOUR modules (mint_api_v2, mint_customer_api, mint_dutchie_sync,
mint_inventory_ops) at their previous versions behind a green deploy. Nothing
surfaced it. `ir_module_module.latest_version` was the only honest signal, and
nothing was looking at it.

This closes that gap: after the upgrade runs, compare every installed module's
recorded version against the manifest actually on the addons path, and say so
loudly when they disagree.

## Modes — ODOO_VERIFY_UPGRADE

  warn   (default) log a loud banner, exit 0. Safe to ship anywhere.
  strict log the banner and exit 1, which fails the container start. Railway
         keeps the PREVIOUS deployment serving, so this marks the deploy failed
         without taking production down — the correct outcome for a deploy that
         did not apply. Turn this on once the estate is known clean.
  off    skip entirely.

A failure of the CHECK ITSELF never blocks a deploy — it warns and exits 0 even
in strict mode. A monitoring bug must not become an outage.

Honours ODOO_DRIFT_EXCLUDE, the same list the pre-upgrade drift scan uses, so a
module that is deliberately pinned behind its manifest does not trip this.

Runnable by hand inside the container, and from a post-deploy job:
    python3 /verify-module-upgrade.py
"""
import ast
import os
import sys

BANNER_TOKEN = "ODOO_UPGRADE_VERIFICATION_FAILED"
SERIES = "19.0"


def clean(v):
    v = (v or "").strip()
    return "" if v.lower() in ("", "false", "none") else v


# --------------------------------------------------------------------------
# Manifest resolution — deliberately identical to the pre-upgrade drift scan.
#
# Resolve through Odoo itself. Hand-rolling a path scan is how the 2026-08-24
# incident happened: Odoo puts the persistent volume (/var/lib/odoo/addons/19.0)
# AHEAD of /opt/extra-addons, and fix-config.sh copies modules onto that volume,
# so a naive scan compares a manifest Odoo is not loading.
# --------------------------------------------------------------------------
ODOO_MOD = None
try:
    from odoo.tools import config as _odoo_config
    _odoo_config.parse_config(["-c", "/var/lib/odoo/odoo.conf"])
    from odoo.modules import module as ODOO_MOD
    ODOO_MOD.initialize_sys_path()
except Exception as exc:  # noqa: BLE001
    print(f"  ! Odoo import failed ({exc}) — falling back to a path scan", file=sys.stderr)
    ODOO_MOD = None

# Fallback only. MUST mirror Odoo's runtime order: the volume before the image.
ADDONS = (
    "/usr/lib/python3/dist-packages/odoo/addons",
    "/var/lib/odoo/addons/19.0",
    "/opt/extra-addons",
    "/usr/lib/python3/dist-packages/addons",
)


def normalise(raw):
    """Mirror Odoo's own adapt_version()."""
    if ODOO_MOD is not None and hasattr(ODOO_MOD, "adapt_version"):
        try:
            return ODOO_MOD.adapt_version(str(raw or "").strip() or "1.0")
        except Exception:  # noqa: BLE001
            pass
    v = str(raw or "").strip()
    if not v:
        return ""
    return v if v.startswith(SERIES + ".") else f"{SERIES}.{v}"


def on_disk_version(name):
    if ODOO_MOD is not None:
        try:
            manifest = ODOO_MOD.get_manifest(name)
        except Exception:  # noqa: BLE001
            return ""
        return normalise(manifest.get("version", "")) if manifest else ""
    for base in ADDONS:
        path = os.path.join(base, name, "__manifest__.py")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                manifest = ast.literal_eval(fh.read())
        except Exception:  # noqa: BLE001
            return ""
        if isinstance(manifest, dict):
            return normalise(manifest.get("version", ""))
    return ""


def db_params():
    """Env first, then the odoo.conf the server itself uses.

    🚨 Bare PORT and USER are NOT database settings on Railway. PORT is the HTTP
    listen port (8080) and USER is the unix user; using them yields
    "port 8080 ... Connection refused". Only the explicit ODOO_DB_* vars and
    odoo.conf are trusted here.
    """
    p = {
        "host": clean(os.environ.get("ODOO_DB_HOST")) or clean(os.environ.get("HOST")),
        "port": clean(os.environ.get("ODOO_DB_PORT")),
        "user": clean(os.environ.get("ODOO_DB_USER")),
        "password": clean(os.environ.get("ODOO_DB_PASSWORD")) or clean(os.environ.get("PASSWORD")),
        "dbname": clean(os.environ.get("ODOO_DB_NAME")),
    }
    cfg = "/var/lib/odoo/odoo.conf"
    if os.path.isfile(cfg):
        import configparser
        cp = configparser.ConfigParser()
        try:
            cp.read(cfg)
            sec = "options" if cp.has_section("options") else (cp.sections()[0] if cp.sections() else None)
            if sec:
                for key, opt in (("host", "db_host"), ("port", "db_port"), ("user", "db_user"),
                                 ("password", "db_password"), ("dbname", "db_name")):
                    if not p[key]:
                        p[key] = clean(cp.get(sec, opt, fallback=""))
        except Exception as exc:  # noqa: BLE001
            print(f"  ! could not read {cfg} ({exc})", file=sys.stderr)
    p["host"] = p["host"] or "localhost"
    p["port"] = p["port"] or "5432"
    p["user"] = p["user"] or clean(os.environ.get("ODOO_DB_USER")) or "odoo"
    p["dbname"] = p["dbname"] or "odoo"
    return p


def version_tuple(v):
    return tuple(int(x) if x.isdigit() else 0 for x in str(v or "").split("."))


def main():
    mode = (os.environ.get("ODOO_VERIFY_UPGRADE") or "warn").strip().lower()
    if mode in ("off", "0", "false", "no"):
        print("=== Upgrade verification disabled (ODOO_VERIFY_UPGRADE=off) ===")
        return 0

    try:
        import psycopg2
    except ImportError:
        print("  ! psycopg2 unavailable — cannot verify upgrade", file=sys.stderr)
        return 0

    try:
        conn = psycopg2.connect(**db_params())
        cur = conn.cursor()
        cur.execute("SELECT name, latest_version FROM ir_module_module WHERE state = 'installed'")
        rows = cur.fetchall()
        cur.close()
        conn.close()
    except Exception as exc:  # noqa: BLE001
        # A broken checker must never block a deploy — warn and pass.
        print(f"  ! upgrade verification could not run: {exc}", file=sys.stderr)
        return 0

    exclude = {m.strip() for m in os.environ.get("ODOO_DRIFT_EXCLUDE", "").split(",") if m.strip()}
    stale, checked, skipped = [], 0, 0
    for name, recorded in rows:
        if name in exclude:
            skipped += 1
            continue
        disk = on_disk_version(name)
        if not disk:
            continue  # not one of ours / no manifest on the addons path
        checked += 1
        recorded = clean(recorded)
        if disk != recorded and version_tuple(disk) > version_tuple(recorded):
            stale.append((name, recorded or "(none)", disk))

    if not stale:
        print(f"=== Upgrade verified: {checked} module(s) match their manifest"
              f"{f', {skipped} excluded' if skipped else ''} ===")
        return 0

    # Loud, greppable, and alertable. The token is the thing to watch for in
    # Loki — "SUCCESS" in Railway will not tell you any of this.
    bar = "!" * 74
    print(bar, file=sys.stderr)
    print(f"!!! {BANNER_TOKEN}", file=sys.stderr)
    print("!!!", file=sys.stderr)
    print(f"!!! {len(stale)} module(s) did NOT upgrade. Odoo is running OLD code for them.", file=sys.stderr)
    print("!!! The container started and the site will serve normally — that is", file=sys.stderr)
    print("!!! exactly why this is easy to miss. Columns for new fields may even", file=sys.stderr)
    print("!!! exist, because Odoo adds those during registry setup without -u.", file=sys.stderr)
    print("!!!", file=sys.stderr)
    for name, was, want in stale:
        print(f"!!!   {name:<32} running {was:<16} manifest {want}", file=sys.stderr)
    print("!!!", file=sys.stderr)
    print("!!! Almost always a ParseError in ONE module's views aborting the whole", file=sys.stderr)
    print("!!! -u batch. Search this log for 'ParseError' — only the FIRST is ever", file=sys.stderr)
    print("!!! reported, so fix them one at a time and redeploy.", file=sys.stderr)
    print(bar, file=sys.stderr)

    if mode == "strict":
        print("!!! ODOO_VERIFY_UPGRADE=strict — failing the container start so this", file=sys.stderr)
        print("!!! deploy is marked FAILED. The previous deployment keeps serving.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
