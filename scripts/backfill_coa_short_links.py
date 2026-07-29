#!/usr/bin/env python3
"""Backfill tracked short links (/r/<4 random alphanumerics>) for certificates
already in the COA document store.

Going forward ``mint_coa_admin`` mints a link at upload time; this catches the
~17.9k certificates that predate that. Safe to re-run: files that already have a
tracker are skipped (keyed on the stored ``url``), so an interrupted run simply
resumes.

Transport is XML-RPC on purpose. This host's ``/jsonrpc`` endpoint reports
``database "odoo" does not exist`` while ``/xmlrpc/2`` authenticates fine
against the same database, so the repo's JSON-RPC helper shape is unusable here.
Uses only the standard library — the repo has no ``node_modules``/``dotenv``.

Two Odoo quirks this works around, both verified against prod:
  * ``code`` passed to ``create()`` is SILENTLY IGNORED (it is a non-stored
    compute whose inverse does not run on create), so each link is created and
    then written.
  * codes are unique-constrained; a taken code raises, and we keep the
    auto-generated code for that one rather than aborting the whole run.

Usage:
    python3 scripts/backfill_coa_short_links.py --dry-run    # report only
    python3 scripts/backfill_coa_short_links.py --limit 25   # small live batch
    python3 scripts/backfill_coa_short_links.py              # full backfill
    python3 scripts/backfill_coa_short_links.py --fix-codes  # re-code anything
                                                             # not already a
                                                             # 4-char code
"""

import argparse
import os
import random
import re
import string
import sys
import xmlrpc.client

# Keep in sync with mint_coa_admin/controllers/main.py
CODE_LENGTH = 4
CODE_ALPHABET = string.ascii_letters + string.digits
CODE_RE = re.compile(r"^[A-Za-z0-9]{%d}$" % CODE_LENGTH)
CHUNK = 200           # ids per RPC round-trip


class CodePool:
    """Hands out unused short codes.

    Every /r/ code shares one namespace with the marketing links, so the pool is
    seeded with ALL existing codes, not just the COA ones. Generating locally
    against that set avoids a per-record round-trip; the DB uniqueness
    constraint is still the backstop if two runs overlap.
    """

    def __init__(self, existing):
        self.used = set(existing)

    def take(self):
        for _ in range(10000):
            code = "".join(random.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
            if code not in self.used:
                self.used.add(code)
                return code
        raise RuntimeError(f"no free {CODE_LENGTH}-char code left")


def load_env():
    """Real environment wins; otherwise read a .env from cwd or the repo root."""
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (".env", os.path.join(here, "..", ".env")):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    m = re.match(r"^\s*([A-Z0-9_]+)\s*=\s*(.*)$", line)
                    if m and not os.environ.get(m.group(1)):
                        os.environ[m.group(1)] = m.group(2).strip().strip("\"'")
            return
        except OSError:
            continue


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    ap.add_argument("--limit", type=int, default=0, help="cap how many links to create")
    ap.add_argument("--fix-codes", action="store_true",
                    help="repair COA links left on a random auto code and exit")
    opts = ap.parse_args()

    load_env()
    url = (os.environ.get("PUBLIC_ODOO_URL") or "https://letsgomint.us").rstrip("/")
    db = os.environ.get("ODOO_DB") or "odoo"
    user = os.environ.get("ODOO_USERNAME") or ""
    key = os.environ.get("ODOO_API_KEY") or ""
    if not key:
        sys.exit("ODOO_API_KEY not set")

    uid = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common").authenticate(db, user, key, {})
    if not uid:
        sys.exit("authentication failed")
    models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")
    print(f"authenticated to {url} as uid {uid}")

    def call(model, method, args, **kw):
        return models.execute_kw(db, uid, key, model, method, args, kw)

    base = call("ir.config_parameter", "get_param", ["web.base.url"]) or url
    root = int(call("ir.config_parameter", "get_param", ["coa_admin.root_dir_id", "17"]))
    print(f"base={base} coaRoot={root}")

    # Every existing code in the shared namespace, so generated codes never
    # collide with a COA link OR a marketing link.
    all_codes = [r["code"] for r in
                 call("link.tracker.code", "search_read", [[]], fields=["code"])]
    pool = CodePool(all_codes)
    print(f"codes already in use (all link types): {len(all_codes)}")

    if opts.fix_codes:
        # Re-code any COA link whose code isn't a 4-char alphanumeric. Covers
        # both the legacy coa-<id> codes and Odoo's own 3-char autos left behind
        # when a code write failed mid-run.
        #
        # NB: `code` is not stored on link.tracker, so it cannot appear in a
        # search domain — read the codes back and filter in Python.
        links = call("link.tracker", "search_read", [[("url", "like", "/web/content/")]],
                     fields=["id", "code", "title"])
        bad = [t for t in links if not CODE_RE.match(str(t.get("code") or ""))]
        print(f"COA links needing a {CODE_LENGTH}-char code: {len(bad)}")
        if not bad:
            return
        if opts.limit > 0:
            bad = bad[:opts.limit]
            print(f"  capped by --limit {opts.limit}")
        fixed = skipped = 0
        for t in bad:
            if opts.dry_run:
                print(f"  would recode {t['code']}  ({t['title']})")
                continue
            try:
                call("link.tracker", "write", [[t["id"]], {"code": pool.take()}])
                fixed += 1
                if fixed % 250 == 0:
                    print(f"  …{fixed}/{len(bad)}")
            except Exception as exc:  # noqa: BLE001
                skipped += 1
                print(f"  FAILED link {t['id']} ({t['title']}): {str(exc)[:100]}")
        print(f"\ndone: recoded={fixed} skipped={skipped}")
        return

    # 1. every certificate under the COA root
    files = call("dms.file", "search_read", [[("directory_id.parent_id", "=", root)]],
                 fields=["id", "name"], order="id")
    print(f"certificates: {len(files)}")

    # 2. their stored PDFs
    att_by_file = {}
    for part in chunks([f["id"] for f in files], CHUNK):
        for a in call("ir.attachment", "search_read",
                      [[("res_model", "=", "dms.file"), ("res_field", "=", "content_file"),
                        ("res_id", "in", part)]],
                      fields=["id", "res_id", "public"]):
            att_by_file[a["res_id"]] = a
    print(f"with a stored PDF: {len(att_by_file)}")

    def url_of(att_id):
        return f"{base}/web/content/{att_id}"

    wanted = [f for f in files if f["id"] in att_by_file]

    # 3. which already have a tracker (resume support)
    already = set()
    for part in chunks(wanted, CHUNK):
        for r in call("link.tracker", "search_read",
                      [[("url", "in", [url_of(att_by_file[f["id"]]["id"]) for f in part])]],
                      fields=["url"]):
            already.add(r["url"])
    print(f"already shortened: {len(already)}")

    todo = [f for f in wanted if url_of(att_by_file[f["id"]]["id"]) not in already]
    if opts.limit > 0:
        todo = todo[:opts.limit]
    print(f"to create: {len(todo)}" + (f" (capped by --limit {opts.limit})" if opts.limit else ""))

    not_public = [f for f in wanted if not att_by_file[f["id"]]["public"]]

    if opts.dry_run:
        print("\n--dry-run: no writes. Sample of what would be created:")
        for f in todo[:5]:
            print(f"  {url_of(att_by_file[f['id']]['id'])}   ({f['name']})")
        print(f"\nattachments needing public=true: {len(not_public)}")
        return

    # 4. the PDFs must be readable without an Odoo session for the link to work
    if not_public:
        for part in chunks([att_by_file[f["id"]]["id"] for f in not_public], CHUNK):
            call("ir.attachment", "write", [part, {"public": True}])
        print(f"made {len(not_public)} attachments public")

    # 5. create, then write the readable code (create ignores `code`)
    created = fell_back = failed = 0
    for f in todo:
        att_id = att_by_file[f["id"]]["id"]
        try:
            new_id = call("link.tracker", "create", [{"url": url_of(att_id), "title": f["name"]}])
            link_id = new_id[0] if isinstance(new_id, list) else new_id
            try:
                call("link.tracker", "write", [[link_id], {"code": pool.take()}])
            except Exception as exc:  # noqa: BLE001
                fell_back += 1
                print(f"  file {f['id']}: code write failed, kept auto code ({str(exc)[:80]})")
            created += 1
            if created % 250 == 0:
                print(f"  …{created}/{len(todo)}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAILED file {f['id']} ({f['name']}): {str(exc)[:120]}")

    print(f"\ndone: created={created} keptAutoCode={fell_back} failed={failed}")


if __name__ == "__main__":
    main()
