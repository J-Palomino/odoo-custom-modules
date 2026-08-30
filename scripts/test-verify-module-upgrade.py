#!/usr/bin/env python3
"""Tests for verify-module-upgrade.py.

Runs anywhere — no Odoo, no database. psycopg2 is faked and the manifest
resolver is stubbed, so this exercises the DECISION logic, which is the part
that matters: when does it stay quiet, when does it shout, and when is it
allowed to fail a deploy.

The two cases worth never regressing:
  * a failure of the CHECK ITSELF exits 0 even in strict mode — a monitoring
    bug must not become an outage
  * a DB version AHEAD of the manifest is not drift; several OCA modules ship
    manifests reading "1.0" while the DB holds a real version, and flagging
    those would make the check cry wolf on every boot

    python3 scripts/test-verify-module-upgrade.py
"""
import io, os, sys, types, importlib.util, contextlib

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'verify-module-upgrade.py')

def run(rows, env=None, db_raises=None, disk=None):
    """Load the script fresh with a faked psycopg2 + manifest resolver."""
    fake = types.ModuleType('psycopg2')
    class _Conn:
        def cursor(self):
            outer = self
            class _C:
                def execute(self, *a, **k): pass
                def fetchall(self): return rows
                def close(self): pass
            return _C()
        def close(self): pass
    def connect(**kw):
        if db_raises: raise Exception(db_raises)
        return _Conn()
    fake.connect = connect
    sys.modules['psycopg2'] = fake

    old_env = dict(os.environ)
    os.environ.pop('ODOO_VERIFY_UPGRADE', None)
    os.environ.pop('ODOO_DRIFT_EXCLUDE', None)
    os.environ.update(env or {})

    spec = importlib.util.spec_from_file_location('vmu_%d' % id(rows), SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if disk is not None:
        mod.on_disk_version = lambda name: disk.get(name, '')

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = mod.main()
    os.environ.clear(); os.environ.update(old_env)
    return code, out.getvalue() + err.getvalue()

FAIL = 0
def check(label, cond):
    global FAIL
    print(('  ok   ' if cond else '  FAIL ') + label)
    if not cond: FAIL += 1

DISK = {'mint_inventory_ops': '19.0.6.0.0', 'mint_api_v2': '19.0.4.29.0'}

# 1. everything matches
c, o = run([('mint_inventory_ops','19.0.6.0.0'), ('mint_api_v2','19.0.4.29.0')], disk=DISK)
check('clean estate -> exit 0', c == 0)
check('clean estate -> says verified', 'Upgrade verified' in o)
check('clean estate -> no banner', 'ODOO_UPGRADE_VERIFICATION_FAILED' not in o)

# 2. drift, default mode
c, o = run([('mint_inventory_ops','19.0.5.0.0'), ('mint_api_v2','19.0.4.29.0')], disk=DISK)
check('drift + warn -> exit 0 (never blocks by default)', c == 0)
check('drift + warn -> banner token present', 'ODOO_UPGRADE_VERIFICATION_FAILED' in o)
check('drift + warn -> names the stale module and both versions',
      'mint_inventory_ops' in o and '19.0.5.0.0' in o and '19.0.6.0.0' in o)
check('drift + warn -> does NOT name the healthy module as stale',
      o.count('mint_api_v2') == 0)

# 3. drift, strict
c, o = run([('mint_inventory_ops','19.0.5.0.0')], env={'ODOO_VERIFY_UPGRADE':'strict'}, disk=DISK)
check('drift + strict -> exit 1 (fails the deploy)', c == 1)
check('drift + strict -> is honest that the boot watcher ignores the code',
      'boot watcher ignores it' in o)

# 4. excluded module is ignored
c, o = run([('mint_inventory_ops','19.0.5.0.0')],
           env={'ODOO_VERIFY_UPGRADE':'strict','ODOO_DRIFT_EXCLUDE':'mint_inventory_ops'}, disk=DISK)
check('excluded module -> exit 0 even in strict', c == 0)
check('excluded module -> counted as excluded', 'excluded' in o)

# 5. DB unreachable must never block
c, o = run([], env={'ODOO_VERIFY_UPGRADE':'strict'}, db_raises='connection refused', disk=DISK)
check('checker failure -> exit 0 even in strict (monitoring bug != outage)', c == 0)

# 6. off
c, o = run([('mint_inventory_ops','19.0.5.0.0')], env={'ODOO_VERIFY_UPGRADE':'off'}, disk=DISK)
check('off -> exit 0, skipped', c == 0 and 'disabled' in o)

# 7. DB AHEAD of disk (downgrade) must NOT trip it
c, o = run([('mint_inventory_ops','19.0.9.0.0')], env={'ODOO_VERIFY_UPGRADE':'strict'}, disk=DISK)
check('db ahead of disk -> not flagged (downgrades are not drift)', c == 0)

# 8. module with no manifest on the path is ignored
c, o = run([('some_oca_module','1.0')], env={'ODOO_VERIFY_UPGRADE':'strict'}, disk=DISK)
check('module with no manifest -> ignored', c == 0)

print(f"\n{'ALL PASS' if not FAIL else str(FAIL)+' FAILED'}")
sys.exit(1 if FAIL else 0)
