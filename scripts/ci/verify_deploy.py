#!/usr/bin/env python3
"""Post-deploy verification for the Odoo modules repo (prod Railway service).

## Why this exists

A push to `main` makes Railway rebuild and restart Odoo, and Railway reports
SUCCESS as soon as the container starts. That says nothing about whether the
module upgrade actually applied. `--update` runs as ONE transaction, so a single
invalid view anywhere in the batch rolls back every module in it while Odoo
keeps serving the old versions behind a green deploy (2026-08-25, 2026-08-28).
`ir_module_module.latest_version` is the only honest signal, and the in-container
watcher (`scripts/verify-module-upgrade.py`) can only log about it — it cannot
fail anything.

This runs in CI after the push and CAN fail:

  1. waits for Railway to deploy THIS commit and reach SUCCESS (catches silently
     dropped auto-deploys and failed builds);
  2. waits for `/web/health` and `/api/v1/health` to answer (the boot-time `-u`
     keeps workers down for a while — 502/524 during that window is expected);
  3. for modules touched by the push:
       - manifest version BUMPED  -> `latest_version` must equal the new version
                                    (else the upgrade rolled back or never ran) -> FAIL
       - views/data/security/migrations changed WITHOUT a bump -> WARN: those
         changes are not applied by a restart
       - module not installed     -> NOTICE (new modules need ODOO_INIT_MODULES)

stdlib only, like scripts/ci/check_modules.py.

## Environment

  RAILWAY_TOKEN     required  Railway account/team token (read) for the Lets Go Mint project
  ODOO_URL          optional  default https://letsgomint.us
  ODOO_DB           required
  ODOO_USERNAME     required  ideally a read-only user
  ODOO_API_KEY      required  that user's API key
  VERIFY_SHA        commit to verify (default GITHUB_SHA)
  BEFORE_SHA        previous head (github.event.before); falls back to the first parent
  VERIFY_EXCLUDE    comma-separated modules to skip (mirror ODOO_DRIFT_EXCLUDE)
  SKIP_SUPERSEDED_CHECK=true  verify an explicit older SHA (manual dispatch)
  GITHUB_TOKEN, GITHUB_REPOSITORY, GITHUB_REF_NAME  optional superseded check
  GITHUB_STEP_SUMMARY  optional markdown report

Exit: 0 verified (or superseded) / 1 verification failed / 2 bad configuration.
"""
import ast
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xmlrpc.client

SERIES = "19.0"
# A named, non-browser UA. letsgomint.us's edge 403s the default Python-urllib
# UA on HTTP routes (verified 2026-09-14) — before Odoo ever sees the request.
UA = "mint-deploy-verify/1 (+github-actions)"
RAILWAY_GRAPHQL = "https://backboard.railway.app/graphql/v2"
ODOO_SERVICE = {
    "name": "Lets Go Mint/Odoo",
    "serviceId": "ce39e6ac-8784-4580-b462-3da0e571668a",
    "environmentId": "1e6ead7c-b733-43c0-9573-dc206928e773",
}
FAIL_STATES = {"FAILED", "CRASHED", "NEEDS_APPROVAL"}
GONE_STATES = {"REMOVED", "REMOVING", "SKIPPED"}
# Files whose changes only take effect through `-u`, not a restart.
UPGRADE_ONLY_PARTS = ("views/", "data/", "security/", "migrations/", "report/", "wizard/")
UPGRADE_ONLY_SUFFIXES = (".xml", ".csv")

REPORT = []


def annotate(level, msg):
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::{level}::{msg}")
    else:
        print(f"[{level}] {msg}")


def env(name, default=None):
    value = (os.environ.get(name) or "").strip()
    return value or default


# ---------------------------------------------------------------------------
# Git: which modules did this push touch, and did their versions change?
# ---------------------------------------------------------------------------

def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def resolve_before(sha):
    before = env("BEFORE_SHA")
    if before and set(before) != {"0"}:
        try:
            git("cat-file", "-e", f"{before}^{{commit}}")
            return before
        except subprocess.CalledProcessError:
            pass
    try:
        return git("rev-parse", f"{sha}^1").strip()
    except subprocess.CalledProcessError:
        return None


def manifest_version(rev, module):
    try:
        raw = git("show", f"{rev}:{module}/__manifest__.py")
    except subprocess.CalledProcessError:
        return None
    try:
        manifest = ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return None
    version = str(manifest.get("version", "") or "").strip() if isinstance(manifest, dict) else ""
    if not version:
        return None
    return version if version.startswith(SERIES + ".") else f"{SERIES}.{version}"


def changed_modules(before, sha):
    """{module: {"files": [...], "old": version|None, "new": version|None}}"""
    if before:
        names = git("diff", "--name-only", before, sha).split()
    else:
        names = git("diff-tree", "--no-commit-id", "--name-only", "-r", sha).split()
    modules = {}
    for path in names:
        top, _, rest = path.partition("/")
        if not rest:
            continue
        modules.setdefault(top, []).append(rest)
    result = {}
    for module, files in sorted(modules.items()):
        new = manifest_version(sha, module)
        if new is None:
            continue  # not an Odoo module at this commit (or deleted)
        result[module] = {"files": files, "old": manifest_version(before, module) if before else None, "new": new}
    return result


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def http_json(url, data=None, headers=None, timeout=20):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode() or "null")


def is_superseded(sha):
    if env("SKIP_SUPERSEDED_CHECK") == "true":
        return False
    token, repo, ref = env("GITHUB_TOKEN"), env("GITHUB_REPOSITORY"), env("GITHUB_REF_NAME")
    if not (token and repo and ref):
        return False
    try:
        _, body = http_json(
            f"https://api.github.com/repos/{repo}/commits/{ref}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )
        head = (body or {}).get("sha")
        return bool(head) and head != sha
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# 1. Railway
# ---------------------------------------------------------------------------

def railway_deployments(token):
    query = (
        'query { deployments(first: 10, input: { serviceId: "%s", environmentId: "%s" }) '
        "{ edges { node { id status createdAt meta } } } }"
        % (ODOO_SERVICE["serviceId"], ODOO_SERVICE["environmentId"])
    )
    _, body = http_json(
        RAILWAY_GRAPHQL,
        data=json.dumps({"query": query}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "x-source": "railway-cli"},
    )
    if body.get("errors"):
        raise RuntimeError(body["errors"][0].get("message"))
    return [e["node"] for e in body["data"]["deployments"]["edges"]]


def wait_for_railway(token, sha, timeout=1800, appear_timeout=600, interval=20):
    started = time.time()
    last_status, api_errors = "", 0
    while time.time() - started < timeout:
        try:
            nodes = railway_deployments(token)
            api_errors = 0
        except Exception as exc:  # noqa: BLE001
            api_errors += 1
            if api_errors >= 5:
                return "failed", f"Railway API kept failing: {exc}"
            time.sleep(interval)
            continue
        mine = next((n for n in nodes if (n.get("meta") or {}).get("commitHash") == sha), None)
        if mine is None:
            if time.time() - started > appear_timeout:
                if is_superseded(sha):
                    return "superseded", "branch head moved on before Railway deployed this commit"
                return "failed", (
                    f"Railway never started a deployment for {sha[:7]} within {appear_timeout}s — "
                    "auto-deploy likely dropped; trigger serviceInstanceDeployV2 with this commitSha"
                )
        else:
            if mine["status"] != last_status:
                print(f"  Railway: {mine['status']} (deployment {mine['id'][:8]})")
                last_status = mine["status"]
            if mine["status"] == "SUCCESS":
                return "success", f"deployment {mine['id'][:8]}"
            if mine["status"] in FAIL_STATES:
                return "failed", f"deployment {mine['id'][:8]} is {mine['status']}"
            if mine["status"] in GONE_STATES:
                newer = any(n["status"] == "SUCCESS" and n["createdAt"] > mine["createdAt"] for n in nodes)
                if newer or is_superseded(sha):
                    return "superseded", f"deployment {mine['id'][:8]} {mine['status']}; replaced by a newer one"
                return "failed", f"deployment {mine['id'][:8]} is {mine['status']} and nothing newer succeeded"
        time.sleep(interval)
    return "failed", f"timed out after {timeout}s (last status: {last_status or 'not started'})"


# ---------------------------------------------------------------------------
# 2. Health
# ---------------------------------------------------------------------------

def wait_for_health(base, timeout=900, interval=15):
    started = time.time()
    last = {}
    while time.time() - started < timeout:
        results = {}
        for path in ("/web/health", "/api/v1/health"):
            try:
                status, _ = http_json(base + path, timeout=15)
                results[path] = status
            except urllib.error.HTTPError as exc:
                results[path] = exc.code
            except Exception as exc:  # noqa: BLE001
                results[path] = type(exc).__name__
        last = results
        if all(v == 200 for v in results.values()):
            return True, results, int(time.time() - started)
        # 502/524 here is normal while the boot-time `-u` holds the workers.
        print(f"  health not ready after {int(time.time() - started)}s: {results}")
        time.sleep(interval)
    return False, last, int(time.time() - started)


# ---------------------------------------------------------------------------
# 3. Module versions over XML-RPC
# ---------------------------------------------------------------------------

class _UATransport(xmlrpc.client.SafeTransport):
    user_agent = UA


def odoo_modules(base, db, login, key, names):
    common = xmlrpc.client.ServerProxy(f"{base}/xmlrpc/2/common", transport=_UATransport())
    uid = common.authenticate(db, login, key, {})
    if not uid:
        raise RuntimeError("Odoo authentication failed (check ODOO_DB / ODOO_USERNAME / ODOO_API_KEY)")
    models = xmlrpc.client.ServerProxy(f"{base}/xmlrpc/2/object", transport=_UATransport())
    rows = models.execute_kw(
        db, uid, key, "ir.module.module", "search_read",
        [[["name", "in", list(names)]]], {"fields": ["name", "state", "latest_version"]},
    )
    return {r["name"]: r for r in rows}


def verify_modules(base, db, login, key, changes, excluded):
    ok = True
    bumped = {m: c for m, c in changes.items() if c["new"] != c["old"] and m not in excluded}
    for module, change in changes.items():
        if module in excluded:
            REPORT.append(f"- ⏭️ `{module}` excluded via VERIFY_EXCLUDE")
            continue
        if module in bumped:
            continue
        upgrade_only = [
            f for f in change["files"]
            if f.startswith(UPGRADE_ONLY_PARTS) or (f.endswith(UPGRADE_ONLY_SUFFIXES) and not f.startswith("static/"))
        ]
        if upgrade_only:
            annotate("warning", f"{module}: {len(upgrade_only)} view/data/security/migration file(s) changed without a manifest version bump — a restart will NOT apply them")
            REPORT.append(f"- ⚠️ `{module}` changed {', '.join(upgrade_only[:4])} without a version bump (not applied until upgraded)")
        else:
            REPORT.append(f"- ✅ `{module}` code-only change (restart loads it; no upgrade needed)")

    if not bumped:
        return ok

    # By the time /web/health answers, the boot-time `--update` has already
    # finished (workers only accept traffic after it). The wait covers replica
    # lag and a slow first worker, not a still-running upgrade.
    deadline = time.time() + int(env("VERIFY_MODULE_TIMEOUT", "180"))
    pending = dict(bumped)
    rows = {}
    while pending and time.time() < deadline:
        rows = odoo_modules(base, db, login, key, pending.keys())
        for module in list(pending):
            row = rows.get(module)
            if row and row["state"] == "installed" and row["latest_version"] == pending[module]["new"]:
                REPORT.append(f"- ✅ `{module}` upgraded to `{pending[module]['new']}` (latest_version)")
                del pending[module]
            elif row is None or row["state"] in ("uninstalled", "uninstallable"):
                annotate("notice", f"{module}: not installed on prod — a NEW module installs only via ODOO_INIT_MODULES")
                REPORT.append(f"- ℹ️ `{module}` not installed (state: {row['state'] if row else 'unknown'}) — install via ODOO_INIT_MODULES")
                del pending[module]
        if pending:
            # Say what we're waiting on — a silent loop reads as a hung job.
            print("  waiting on module versions: " + ", ".join(
                f"{m} latest_version={(rows.get(m) or {}).get('latest_version')!r} want={c['new']!r}"
                for m, c in pending.items()
            ))
            time.sleep(30)

    for module, change in pending.items():
        row = rows.get(module) or {}
        ok = False
        annotate("error", f"{module}: latest_version is {row.get('latest_version')!r}, manifest is {change['new']!r} — the upgrade rolled back or never ran. Check the deploy log for ParseError / 'Failed to load registry', and that the module is in ODOO_UPDATE_MODULES or not excluded from drift")
        REPORT.append(f"- ❌ `{module}` latest_version `{row.get('latest_version')}` ≠ manifest `{change['new']}` (state {row.get('state')})")
    return ok


# ---------------------------------------------------------------------------

def finish(ok, sha):
    REPORT.insert(0, f"### Odoo deploy verification — `{sha[:7]}`\n")
    REPORT.append("")
    REPORT.append("**Result: verified**" if ok else "**Result: FAILED**")
    text = "\n".join(REPORT)
    print(text)
    summary = env("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    sys.exit(0 if ok else 1)


def main():
    sha = env("VERIFY_SHA") or env("GITHUB_SHA")
    token = env("RAILWAY_TOKEN")
    base = (env("ODOO_URL", "https://letsgomint.us")).rstrip("/")
    db, login, key = env("ODOO_DB"), env("ODOO_USERNAME"), env("ODOO_API_KEY")
    missing = [n for n, v in (("RAILWAY_TOKEN", token), ("ODOO_DB", db), ("ODOO_USERNAME", login), ("ODOO_API_KEY", key)) if not v]
    if not sha or len(sha) != 40:
        annotate("error", "VERIFY_SHA/GITHUB_SHA must be a full 40-char commit SHA")
        sys.exit(2)
    if missing:
        annotate("error", f"Missing required secret(s): {', '.join(missing)} — cannot verify the Odoo deploy")
        sys.exit(2)
    excluded = {m.strip() for m in (env("VERIFY_EXCLUDE", "") or "").split(",") if m.strip()}

    before = resolve_before(sha)
    changes = changed_modules(before, sha)
    print(f"Verifying {sha[:7]} (diff base {before[:7] if before else 'none'}): {len(changes)} module(s) touched")

    state, detail = wait_for_railway(token, sha)
    if state == "superseded":
        annotate("notice", f"{sha[:7]} was superseded by a newer push; its own run verifies it")
        REPORT.append(f"- ⏭️ superseded — {detail}")
        finish(True, sha)
    if state == "failed":
        annotate("error", f"Railway: {detail}")
        REPORT.append(f"- ❌ Railway: {detail}")
        finish(False, sha)
    REPORT.append(f"- ✅ Railway SUCCESS ({detail})")

    healthy, results, waited = wait_for_health(base)
    if not healthy:
        annotate("error", f"Odoo never became healthy after the deploy: {results}")
        REPORT.append(f"- ❌ health after {waited}s: {results}")
        finish(False, sha)
    REPORT.append(f"- ✅ `/web/health` + `/api/v1/health` 200 (after {waited}s)")

    if not changes:
        REPORT.append("- ✅ no Odoo modules touched by this push")
        finish(True, sha)
    ok = verify_modules(base, db, login, key, changes, excluded)
    finish(ok, sha)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        annotate("error", f"verify_deploy crashed: {exc}")
        sys.exit(1)
