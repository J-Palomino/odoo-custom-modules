#!/usr/bin/env python3
"""Static validation for the Odoo addons in this repo.

Stdlib only, no Odoo import, no database -- so it runs in seconds on a bare
GitHub runner. Every rule here exists because the corresponding mistake has
actually shipped from this repo at least once.

Two tiers:

  HARD    Currently zero across the tree, so any new occurrence fails the build.
  RATCHET Pre-existing violations are recorded in ci-baseline.json. The count may
          shrink but never grow -- same pattern as the storefront repo's
          ui-boundary and typecheck ratchets. Fix them opportunistically; you are
          only blocked from adding more.

Usage:
    python3 scripts/ci/check_modules.py             # validate (CI entrypoint)
    python3 scripts/ci/check_modules.py --update    # re-record the ratchet baseline
"""
from __future__ import annotations

import argparse
import ast
import json
import pathlib
import re
import sys
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parents[2]
BASELINE = ROOT / "ci-baseline.json"

# Nested worktrees/caches are not part of the tree under test. A stray
# .worktrees/ once made the storefront's lint walk an entire second checkout.
SKIP_PARTS = {".git", "node_modules", "__pycache__", ".worktrees", ".claude", ".github"}


def walk(pattern: str):
    for path in ROOT.rglob(pattern):
        parts = path.relative_to(ROOT).parts
        if any(p in SKIP_PARTS for p in parts):
            continue
        if any(p.startswith(".wt-") or p.startswith("_wt-") for p in parts):
            continue
        yield path


def rel(path: pathlib.Path) -> str:
    return str(path.relative_to(ROOT))


def manifests() -> list[pathlib.Path]:
    return sorted(walk("__manifest__.py"))


# ── HARD checks ──────────────────────────────────────────────────────────────

def check_python_syntax() -> list[str]:
    """A syntax error reaches the image and Odoo fails to load the module."""
    out = []
    for path in walk("*.py"):
        try:
            ast.parse(path.read_bytes(), filename=str(path))
        except SyntaxError as exc:
            out.append(f"{rel(path)}:{exc.lineno}: {exc.msg}")
    return out


def check_xml_wellformed() -> list[str]:
    """Malformed XML aborts the whole `-u` and rolls the upgrade back."""
    out = []
    for path in walk("*.xml"):
        try:
            ET.parse(path)
        except ET.ParseError as exc:
            out.append(f"{rel(path)}: {exc}")
    return out


def check_manifest_parses() -> list[str]:
    """__manifest__.py must literal-eval to a dict -- Odoo reads it that way."""
    out = []
    for path in manifests():
        try:
            value = ast.literal_eval(path.read_text())
        except Exception as exc:  # noqa: BLE001 - report whatever it was
            out.append(f"{rel(path)}: {exc}")
            continue
        if not isinstance(value, dict):
            out.append(f"{rel(path)}: evaluates to {type(value).__name__}, not dict")
    return out


def check_manifest_data_files() -> list[str]:
    """Every file listed in data/demo must exist, or the module install dies."""
    out = []
    for path in manifests():
        try:
            value = ast.literal_eval(path.read_text())
        except Exception:
            continue  # already reported by check_manifest_parses
        module = path.parent
        for key in ("data", "demo"):
            for entry in value.get(key) or []:
                if not (module / entry).exists():
                    out.append(f"{rel(path)}: {key} references missing {entry}")
    return out


def check_manifest_asset_files() -> list[str]:
    """Asset paths of the form '<module>/static/...' must resolve.

    A dangling asset path surfaces only at bundle-build time, in the browser, as
    a misleading resolver error -- long after the merge.

    Only paths whose first segment is a module *in this repo* are checked: a
    manifest may legitimately reference core Odoo addons (web/static/lib/...)
    that live in the image, not here, and we cannot resolve those from a
    checkout.
    """
    local_modules = {p.parent.name for p in manifests()}
    out = []
    for path in manifests():
        try:
            value = ast.literal_eval(path.read_text())
        except Exception:
            continue
        for bundle, entries in (value.get("assets") or {}).items():
            for entry in entries:
                if isinstance(entry, (tuple, list)):  # ('replace', a, b) etc.
                    continue
                if not isinstance(entry, str) or entry.startswith(("http:", "https:", "/")):
                    continue
                if any(ch in entry for ch in "*?["):  # globs are legitimate
                    continue
                head = entry.split("/", 1)[0]
                if head not in local_modules:
                    continue  # core or out-of-repo addon; not ours to verify
                if not (ROOT / entry).exists():
                    out.append(f"{rel(path)}: assets[{bundle!r}] missing {entry}")
    return out


def check_renamed_fields() -> list[str]:
    """Odoo 19 renames that fail loudly at install time."""
    renames = {
        r"\bgroups_id\s*=": "groups_id -> group_ids (Odoo 19)",
        r"\battrs\s*=\s*[\"']": "attrs= was removed in Odoo 17+",
    }
    out = []
    for path in walk("*.py"):
        text = path.read_text(errors="ignore")
        for pattern, message in renames.items():
            for match in re.finditer(pattern, text):
                line = text[: match.start()].count("\n") + 1
                out.append(f"{rel(path)}:{line}: {message}")
    return out


def check_migration_dirs() -> list[str]:
    """A migration dir that matches no manifest version silently never runs.

    Odoo only executes scripts under migrations/<version>/ when <version> is
    greater than the installed version and <= the manifest version, so a typo in
    the directory name is a no-op that looks exactly like success.
    """
    out = []
    for path in manifests():
        migrations = path.parent / "migrations"
        if not migrations.is_dir():
            continue
        try:
            version = str(ast.literal_eval(path.read_text()).get("version", ""))
        except Exception:
            continue
        for child in sorted(migrations.iterdir()):
            if not child.is_dir() or child.name == "__pycache__":
                continue
            if not re.fullmatch(r"[0-9]+(\.[0-9]+)*", child.name):
                out.append(f"{rel(child)}: not a version-shaped directory name")
                continue
            if _parse_version(child.name) > _parse_version(version):
                out.append(
                    f"{rel(child)}: version is ahead of the manifest "
                    f"({child.name} > {version}) so it will never run"
                )
            scripts = [p.name for p in child.glob("*.py")]
            if not any(s.startswith(("pre-", "post-", "end-")) for s in scripts):
                out.append(
                    f"{rel(child)}: no pre-/post-/end-migrate.py -- Odoo runs nothing here"
                )
    return out


def _parse_version(text: str) -> tuple[int, ...]:
    """Numeric compare, so 1.0.10 > 1.0.9 (string compare gets this backwards)."""
    return tuple(int(p) for p in text.split(".") if p.isdigit())


# ── RATCHET checks ───────────────────────────────────────────────────────────

def ratchet_group_in_search() -> list[str]:
    """<group> inside a <search> view is fatal on Odoo 19.

    It invalidates the view, which aborts and rolls back the ENTIRE module
    upgrade -- behind a green deploy, so nothing surfaces until someone notices
    the module is still on the old version. This rolled mint_api_v2 back twice.
    Group-by filters belong directly in <search>.
    """
    out = []
    for path in walk("*.xml"):
        try:
            tree = ET.parse(path)  # ET drops comments, so prose about the rule is safe
        except ET.ParseError:
            continue  # reported by the hard XML check
        for search in tree.iter("search"):
            if next(search.iter("group"), None) is not None:
                out.append(rel(path))
                break
    return out


def ratchet_sql_constraints() -> list[str]:
    """_sql_constraints is IGNORED on Odoo 19 -- constraints silently unenforced.

    Replace with models.Constraint. Dangerous precisely because nothing errors:
    the constraint simply stops existing at the database level.
    """
    out = []
    for path in walk("*.py"):
        text = path.read_text(errors="ignore")
        for match in re.finditer(r"^\s*_sql_constraints\s*=", text, re.M):
            line = text[: match.start()].count("\n") + 1
            out.append(f"{rel(path)}:{line}")
    return out


def ratchet_dockerfile_copy() -> list[str]:
    """A module with no COPY line never reaches /opt/extra-addons in the image.

    It is then invisible to Odoo no matter what is committed: update_list()
    returns 0 updates and the module cannot be installed. Cost a second
    commit+deploy cycle on 2026-04-22.
    """
    dockerfile = ROOT / "Dockerfile"
    if not dockerfile.exists():
        return []
    copied = set(
        re.findall(
            r"^COPY\s+(?:--chown=\S+\s+)?(\S+)\s+/opt/extra-addons/",
            dockerfile.read_text(),
            re.M,
        )
    )
    present = {p.parent.name for p in ROOT.glob("*/__manifest__.py")}
    return sorted(present - copied)


HARD_CHECKS = {
    "python-syntax": check_python_syntax,
    "xml-wellformed": check_xml_wellformed,
    "manifest-parses": check_manifest_parses,
    "manifest-data-files": check_manifest_data_files,
    "manifest-asset-files": check_manifest_asset_files,
    "renamed-fields": check_renamed_fields,
    "migration-dirs": check_migration_dirs,
}

RATCHET_CHECKS = {
    "group-in-search": ratchet_group_in_search,
    "sql-constraints": ratchet_sql_constraints,
    "dockerfile-copy": ratchet_dockerfile_copy,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update",
        action="store_true",
        help="re-record the ratchet baseline (say why in the commit message)",
    )
    args = parser.parse_args()

    failed = False

    print("== hard checks (must stay at zero) ==")
    for name, fn in HARD_CHECKS.items():
        problems = fn()
        print(f"  {'FAIL' if problems else 'ok  '}  {name}: {len(problems)}")
        for problem in problems[:20]:
            print(f"          {problem}")
        if len(problems) > 20:
            print(f"          ... and {len(problems) - 20} more")
        if problems:
            failed = True

    counts = {name: len(fn()) for name, fn in RATCHET_CHECKS.items()}

    if args.update:
        BASELINE.write_text(json.dumps(counts, indent=2, sort_keys=True) + "\n")
        print(f"\nbaseline written to {rel(BASELINE)}: {counts}")
        return 0

    baseline = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
    print("\n== ratchet checks (may shrink, never grow) ==")
    for name, count in counts.items():
        allowed = baseline.get(name)
        if allowed is None:
            print(f"  FAIL  {name}: {count} (no baseline recorded -- run --update)")
            failed = True
        elif count > allowed:
            print(f"  FAIL  {name}: {count} > baseline {allowed} (+{count - allowed} new)")
            for problem in RATCHET_CHECKS[name]()[:20]:
                print(f"          {problem}")
            failed = True
        elif count < allowed:
            print(f"  ok    {name}: {count} < baseline {allowed} -- improved, run --update")
        else:
            print(f"  ok    {name}: {count} (at baseline)")

    print("\nFAILED" if failed else "\nPASSED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
