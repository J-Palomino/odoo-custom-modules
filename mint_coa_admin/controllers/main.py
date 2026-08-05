"""COA Document Store — staff admin panel, served natively by Odoo.

Ported from the standalone Astro/Cloudflare panel. Because it runs inside Odoo
under ``auth='user'``, the visitor authenticates with their normal Odoo session
(including "Sign in with Google" on the Odoo login page), every operation runs
as ``request.env.user`` so Odoo's own DMS access rules decide what they may
touch, and edits are audited to them. Only the narrow COA access-group
membership writes are elevated with ``sudo()`` — and only after proving the
caller is themselves a COA member.

Two behaviours here exist to keep a certificate's public links trustworthy:

* Names are checked against the /coa search key before any write, because two
  certificates that normalise alike are indistinguishable to a customer
  searching their batch number (see the ``_search_key`` block below).
* ``replace`` writes new bytes onto an existing dms.file rather than filing a
  second one, so the certificate's ``/r/<code>`` short link — and any QR already
  printed on a package — keeps resolving to the corrected PDF.
"""

import base64
import logging
import os
import random
import re
import string

from odoo import http
from odoo.exceptions import AccessError, UserError
from odoo.http import request

_logger = logging.getLogger(__name__)

# Short links read as /r/<4 random alphanumerics>, e.g. /r/k9Pq.
#
# 4 chars of base62 is 14,776,336 combinations against ~18k certificates
# (~0.1% occupancy), so random assignment effectively never collides; the
# unique constraint on link.tracker.code is the backstop either way. All /r/
# codes share ONE namespace with the marketing links, so uniqueness is checked
# globally, not per-prefix.
#
# Keep in sync with scripts/backfill_coa_short_links.py.
COA_CODE_LENGTH = 4
COA_CODE_ALPHABET = string.ascii_letters + string.digits
COA_CODE_RE = re.compile(r"^[A-Za-z0-9]{%d}$" % COA_CODE_LENGTH)
COA_CODE_ATTEMPTS = 8

# Rows returned by search_files; the UI reports when it caps.
SEARCH_LIMIT = 200

_HTML_PATH = os.path.join(os.path.dirname(__file__), "..", "static", "src", "index.html")
_HTML_CACHE = None


def _page_html():
    global _HTML_CACHE
    if _HTML_CACHE is None:
        with open(_HTML_PATH, "r", encoding="utf-8") as fh:
            _HTML_CACHE = fh.read()
    return _HTML_CACHE


def _sanitize(name):
    """Match the panel's original filename hygiene: no slashes, collapse
    whitespace, cap length."""
    return " ".join(str(name or "").replace("/", "-").split())[:200]


# --------------------------------------------------------------- name clashes
# Two certificates whose names collide inside one brand folder are
# indistinguishable to a customer: the /coa index derives its batch tokens from
# the filename and normalises them before matching, so both answer the same
# batch search as "exact" hits, auto-open stops, and whoever scanned the label
# has to guess. Odoo's dms `_check_name` constraint only rejects byte-identical
# names, so "ABC B12.pdf", "abc b12.pdf" and "ABC-B12.pdf" all get through it.
#
# "Collide" is therefore defined by the search index rather than by string
# equality. Keep in sync with src/lib/coa/names.ts in the letsgomint-us repo,
# which enforces the identical rule for the Astro panel.

_NON_ALNUM_RE = re.compile(r"[^A-Z0-9]")


def _search_key(name):
    """The form /coa actually matches on: uppercase, alphanumerics only.

    Mirrors ``norm()`` in src/pages/coa.astro — if two names share this key, no
    batch search can tell them apart.
    """
    return _NON_ALNUM_RE.sub("", str(name or "").upper())


def _stem(name):
    """Split off the extension so "ABC.pdf" and "ABC(2).pdf" stay comparable."""
    base, dot, ext = str(name).rpartition(".")
    if not dot or not base:  # no extension, or a leading-dot name like ".x"
        return str(name), ""
    return base, "." + ext


# 4 characters is short enough to sit inside a batch run yet long enough to
# stay selective across ~18k filenames.
_WINDOW = 4
_WINDOW_STRIDE = 2
_MAX_WINDOWS = 8


def _search_fragments(query):
    """Coarse ``ilike`` fragments for a staff search — the SQL half of the lookup.

    The precise test is ``_matches_query``, comparing on the search key, but
    that cannot be expressed as an Odoo domain, so a candidate set has to be
    fetched first. The one rule that matters: a fragment must never exclude a
    row the precise test would accept. A fragment does that whenever it
    straddles a separator in the stored filename — "22RED01062026ACLR" is not
    ``ilike``-found in "22RED 01062026ACLR.pdf" even though it matches on the
    search key.

    So fragments only ever come from *within* one alphanumeric run:

    * several runs (the staffer typed separators) -> AND the runs; each is
      separator-free and the filename must contain all of them somewhere;
    * one run (typed unpunctuated) -> OR short sliding windows of it, since we
      cannot know where the filename puts its separators. Any window landing
      inside a run of the filename retrieves it.

    Returns ``(fragments, mode)``; an empty list means no coarse filter.
    """
    runs = [r for r in re.findall(r"[A-Za-z0-9]+", str(query or "")) if len(r) >= 2]
    if len(runs) >= 2:
        # The three longest are the most selective; more just costs query time.
        return sorted(runs, key=len, reverse=True)[:3], "and"
    run = runs[0] if runs else ""
    if not run:
        return [], "and"
    if len(run) <= _WINDOW:
        return [run], "and"
    windows = []
    i = 0
    while i + _WINDOW <= len(run) and len(windows) < _MAX_WINDOWS:
        windows.append(run[i:i + _WINDOW])
        i += _WINDOW_STRIDE
    return windows, "or"


def _matches_query(name, query):
    """Does this filename match what a staffer typed? Compared on the search
    key, so the panel finds a certificate by the same batch number that works
    on /coa — punctuation, case and spacing are irrelevant on both sides."""
    q = _search_key(query)
    return bool(q) and q in _search_key(name)


def _clash_in(rows, name, exclude_id=0):
    """The row already holding this name, ignoring ``exclude_id``."""
    target = _search_key(name)
    if not target:
        return None
    for row in rows:
        if row["id"] != exclude_id and _search_key(row["name"]) == target:
            return row
    return None


def _unique_name(rows, name):
    """"ABC.pdf" -> "ABC(2).pdf", matching the dms module's own copy
    convention (see dms/tools/file.py:compute_name)."""
    base, ext = _stem(name)
    for i in range(2, 500):
        candidate = "%s(%d)%s" % (base, i, ext)
        if not _clash_in(rows, candidate):
            return candidate
    raise UserError("Too many certificates already share that name")


class CoaAdmin(http.Controller):
    # Prod ids by default; overridable per-env (e.g. staging fixtures) via
    # ir.config_parameter so the module is testable where the COA data differs.
    def _root_dir_id(self):
        return int(request.env["ir.config_parameter"].sudo().get_param("coa_admin.root_dir_id", "17"))

    def _access_group_id(self):
        return int(request.env["ir.config_parameter"].sudo().get_param("coa_admin.access_group_id", "3"))

    def _member_ids(self):
        """User ids in the COA access group (read elevated — ordinary members
        can't read the group record itself)."""
        grp = request.env["dms.access.group"].sudo().browse(self._access_group_id())
        return grp.exists() and grp.users.ids or []

    def _require_member(self):
        """Raise unless the signed-in user is a current COA member."""
        if request.env.uid not in self._member_ids():
            raise AccessError(
                "Your account doesn't have COA access. Ask an admin to add you "
                "under “Manage access.”"
            )

    def _attachment_map(self, file_ids):
        """file_id -> content_file attachment id, read as the acting user."""
        out = {}
        if not file_ids:
            return out
        atts = request.env["ir.attachment"].search_read(
            [("res_model", "=", "dms.file"), ("res_field", "=", "content_file"),
             ("res_id", "in", file_ids)],
            ["id", "res_id"], limit=len(file_ids))
        for a in atts:
            out[a["res_id"]] = a["id"]
        return out

    def _names_in_directory(self, directory_id):
        """Every name in a directory, for ``_clash_in`` to compare on.

        Deliberately unfiltered: an ``ilike`` seed cannot find the collisions
        that matter, because "WYLD-B126.pdf" is not LIKE "WYLD B126" yet both
        reduce to the same batch token. Same shape and limit as the `list_files`
        query the panel already runs whenever a folder is opened.
        """
        return request.env["dms.file"].search_read(
            [("directory_id", "=", directory_id)], ["id", "name"],
            limit=5000, order="id asc")

    def _replace_content(self, rec, content_b64):
        """Write new bytes onto an existing dms.file and return the panel row.

        The dms.file id is preserved, and with it the ir.attachment behind
        content_file: Odoo rewrites that attachment in place rather than making
        a new one (verified on production — the attachment id is unchanged and
        `public` survives, only the store_fname revision bumps). That is what
        keeps a certificate's /r/<code> short link — and any QR already printed
        on a package — resolving to the corrected PDF, because the tracker is
        keyed on /web/content/<attachment id>. Uploading a second copy instead
        mints a NEW attachment, hence a NEW code, orphaning the printed one.
        """
        rec.write({"content": content_b64})
        att_id = self._attachment_map([rec.id]).get(rec.id)
        short_url = None
        if att_id:
            att = request.env["ir.attachment"].browse(att_id)
            if not att.public:
                att.write({"public": True})
            try:
                # Find-or-create: the target URL is unchanged, so this returns
                # the certificate's existing link rather than minting a new one.
                short_url = self._mint_short_link(att_id, rec.name, rec.id).short_url
            except Exception:  # noqa: BLE001
                _logger.exception("COA replace %s: short link lookup failed", rec.id)
        return {"ok": True, "fileId": rec.id, "attachmentId": att_id,
                "shortUrl": short_url, "replaced": True}

    # --------------------------------------------------------- short links
    # Staff hand COA links to customers, so each certificate gets a tracked
    # /r/<code> short link instead of a raw /web/content/<id> URL — same Odoo
    # Link Tracker the rest of the business uses, so clicks are counted.
    #
    # link.tracker is reached with sudo() deliberately: per the standing
    # agents-only policy the tracker is not exposed to ordinary internal users
    # (no group, no menus), so a COA member has no rights on the model. This
    # mirrors the access-management actions below — elevate only after
    # _require_member() has proved the caller is a COA member, and only for
    # this one narrow operation.

    def _public_base(self):
        """Canonical public base for shareable links. host_url is whatever host
        the staffer happens to be on; the short link's target must be the public
        site, so prefer web.base.url."""
        base = request.env["ir.config_parameter"].sudo().get_param("web.base.url")
        return (base or request.httprequest.host_url).rstrip("/")

    def _content_url(self, attachment_id):
        return "%s/web/content/%s" % (self._public_base(), attachment_id)

    def _assign_short_code(self, link):
        """Give `link` a fresh 4-char alphanumeric code that is not already in
        use. Returns True if one was assigned.

        Each attempt runs in its own savepoint: the uniqueness check is a
        validation raised on write, and without a savepoint a failed attempt
        would poison the surrounding transaction and take the whole request
        with it.
        """
        taken = request.env["link.tracker.code"].sudo()
        for _attempt in range(COA_CODE_ATTEMPTS):
            code = "".join(random.choice(COA_CODE_ALPHABET) for _ in range(COA_CODE_LENGTH))
            if taken.search_count([("code", "=", code)]):
                continue
            try:
                with request.env.cr.savepoint():
                    link.write({"code": code})
                return True
            except Exception:  # noqa: BLE001
                # Lost a race between the check and the write — try another.
                continue
        _logger.warning(
            "COA short link %s: no free %d-char code after %d attempts, keeping %s",
            link.id, COA_CODE_LENGTH, COA_CODE_ATTEMPTS, link.code)
        return False

    def _mint_short_link(self, attachment_id, title, file_id=None):
        """Find-or-create the tracked link for one certificate, return the
        link.tracker record.

        Idempotent: keyed on the stored `url`, so calling twice returns the same
        record rather than a duplicate.
        """
        target = self._content_url(attachment_id)
        tracker = request.env["link.tracker"].sudo()
        link = tracker.search([("url", "=", target)], limit=1)
        if link:
            return link

        # `code` passed to create() is silently ignored — it is a non-stored
        # compute whose inverse doesn't run on create — so set it with a
        # follow-up write(), which replaces the auto-generated code in place.
        # If every attempt fails we keep Odoo's own 3-char code rather than
        # losing the link entirely.
        link = tracker.create({"url": target, "title": title})
        self._assign_short_code(link)
        return link

    def _short_link_map(self, attachment_ids):
        """attachment id -> existing short URL, for the ones already shortened.

        One batched search so listing a folder costs a single query and does NOT
        mint trackers as a side effect of browsing — links are created on demand
        by the `short_link` action. Keyed on `url` because `code`/`short_url` are
        non-stored computes and cannot be searched.
        """
        out = {}
        if not attachment_ids:
            return out
        by_url = {self._content_url(a): a for a in attachment_ids}
        rows = request.env["link.tracker"].sudo().search_read(
            [("url", "in", list(by_url))], ["url", "short_url"])
        for r in rows:
            att = by_url.get(r["url"])
            if att and r.get("short_url"):
                out[att] = r["short_url"]
        return out

    # ------------------------------------------------------------------ page
    @http.route("/coa/admin", type="http", auth="user", website=False, csrf=False)
    def page(self, **kw):
        # auth='user' already forced a login (Odoo's own page, Google included).
        return request.make_response(
            _page_html(),
            headers=[("Content-Type", "text/html; charset=utf-8"),
                     ("X-Robots-Tag", "noindex, nofollow")],
        )

    # ------------------------------------------------------------------- api
    @http.route("/coa/admin/api", type="json", auth="user")
    def api(self, action=None, **params):
        # 'init' tells the UI whether to show the app or a no-access notice.
        if action == "init":
            return {
                "member": request.env.uid in self._member_ids(),
                "name": request.env.user.name,
                "odoo_base": request.httprequest.host_url.rstrip("/"),
            }

        # Everything else requires COA membership.
        self._require_member()
        root = self._root_dir_id()

        if action == "list_brands":
            dirs = request.env["dms.directory"].search_read(
                [("parent_id", "=", root)], ["id", "name", "count_files"],
                limit=1000, order="name")
            return {"brands": dirs}

        if action == "list_files":
            directory_id = int(params.get("directoryId") or 0)
            if not directory_id:
                raise UserError("directoryId required")
            files = request.env["dms.file"].search_read(
                [("directory_id", "=", directory_id)],
                ["id", "name", "human_size", "write_date"],
                limit=5000, order="name")
            att = self._attachment_map([f["id"] for f in files])
            short = self._short_link_map([a for a in att.values() if a])
            return {"files": [{
                "id": f["id"], "name": f["name"], "size": f["human_size"],
                "updated": f["write_date"], "attachmentId": att.get(f["id"]),
                "shortUrl": short.get(att.get(f["id"])),
            } for f in files]}

        if action == "search_files":
            # Cross-brand lookup. The panel is otherwise browse-only, and 86% of
            # certificates sit in folders holding more than a hundred — finding
            # the one to replace by scrolling STIIIZY's 1,412 rows is not a
            # workflow. Matching is on the /coa search key, so a batch number
            # off a package finds the same certificate the customer sees.
            query = (params.get("query") or "").strip()
            if len(_search_key(query)) < 3:
                return {"files": [], "query": query, "truncated": False, "total": 0}
            domain = [("root_directory_id", "=", root)]
            fragments, mode = _search_fragments(query)
            if mode == "or" and len(fragments) > 1:
                # Odoo prefix notation: n-1 "|" operators in front of n leaves.
                domain.extend(["|"] * (len(fragments) - 1))
            domain.extend([("name", "ilike", f) for f in fragments])
            candidates = request.env["dms.file"].search_read(
                domain, ["id", "name", "human_size", "write_date", "directory_id"],
                limit=2000, order="name")
            hits = [f for f in candidates if _matches_query(f["name"], query)]
            capped = hits[:SEARCH_LIMIT]
            att = self._attachment_map([f["id"] for f in capped])
            short = self._short_link_map([a for a in att.values() if a])
            return {
                "query": query,
                # Say so rather than silently showing a subset — a staffer who
                # cannot see their certificate needs to know the list was cut.
                "truncated": len(hits) > len(capped),
                "total": len(hits),
                "files": [{
                    "id": f["id"], "name": f["name"], "size": f["human_size"],
                    "updated": f["write_date"], "attachmentId": att.get(f["id"]),
                    "shortUrl": short.get(att.get(f["id"])),
                    "directoryId": f["directory_id"] and f["directory_id"][0] or None,
                    "brand": (f["directory_id"] and f["directory_id"][1].split(" / ")[-1] or "").strip(),
                } for f in capped],
            }

        if action == "upload":
            directory_id = int(params.get("directoryId") or 0)
            name = _sanitize(params.get("name"))
            content_b64 = params.get("contentBase64") or ""
            # "error" (default) reports the clash so the UI can ask; "replace"
            # keeps the certificate's existing short link and swaps the bytes;
            # "keep_both" files it alongside under "name(2).pdf" — a NEW link,
            # so only for genuinely distinct certificates.
            on_conflict = params.get("onConflict") or "error"
            if not directory_id or not name or not content_b64:
                raise UserError("directoryId, name and contentBase64 required")

            siblings = self._names_in_directory(directory_id)
            clash = _clash_in(siblings, name)
            final_name = name
            if clash:
                if on_conflict == "replace":
                    return self._replace_content(
                        request.env["dms.file"].browse(clash["id"]), content_b64)
                if on_conflict == "keep_both":
                    # Number off the name already in the folder, not the one just
                    # typed, so one casing/spelling per folder wins and the name
                    # the UI offered is the name that gets created.
                    final_name = _unique_name(siblings, clash["name"])
                else:
                    # A normal outcome for the UI to act on, not an exception:
                    # raising would lose the structured payload.
                    return {
                        "ok": False,
                        "code": "name_exists",
                        "fileId": clash["id"],
                        "name": clash["name"],
                        "suggestedName": _unique_name(siblings, clash["name"]),
                    }

            rec = request.env["dms.file"].create({
                "name": final_name, "directory_id": directory_id, "content": content_b64,
            })
            att = self._attachment_map([rec.id]).get(rec.id)
            short_url = None
            if att:
                # Make the content public so /web/content and /coa can serve it.
                request.env["ir.attachment"].browse(att).write({"public": True})
                # Every certificate gets its tracked short link at upload time,
                # so staff never have to mint one before sharing.
                try:
                    short_url = self._mint_short_link(att, rec.name, rec.id).short_url
                except Exception:  # noqa: BLE001
                    # A tracker failure must not lose the uploaded PDF — the row
                    # simply falls back to its "Shorten" button.
                    _logger.exception("COA upload %s: short link minting failed", rec.id)
            return {"ok": True, "fileId": rec.id, "attachmentId": att,
                    "shortUrl": short_url, "name": rec.name}

        if action == "replace":
            # Corrected PDF for a certificate that is already published: same
            # dms.file, same attachment, so the same /r/<code> and the same QR.
            # Use this rather than re-uploading, which mints a second link.
            file_id = int(params.get("fileId") or 0)
            content_b64 = params.get("contentBase64") or ""
            if not file_id or not content_b64:
                raise UserError("fileId and contentBase64 required")
            rec = request.env["dms.file"].browse(file_id)
            if not rec.exists():
                raise UserError("That certificate no longer exists")
            return self._replace_content(rec, content_b64)

        if action == "rename":
            file_id = int(params.get("fileId") or 0)
            name = _sanitize(params.get("name"))
            if not file_id or not name:
                raise UserError("fileId and name required")
            rec = request.env["dms.file"].browse(file_id)
            if not rec.exists():
                raise UserError("That certificate no longer exists")
            clash = _clash_in(
                self._names_in_directory(rec.directory_id.id), name, file_id)
            if clash:
                return {"ok": False, "code": "name_exists",
                        "fileId": clash["id"], "name": clash["name"]}
            rec.write({"name": name})
            return {"ok": True, "name": name}

        if action == "move":
            file_id = int(params.get("fileId") or 0)
            directory_id = int(params.get("directoryId") or 0)
            if not file_id or not directory_id:
                raise UserError("fileId and directoryId required")
            rec = request.env["dms.file"].browse(file_id)
            if not rec.exists():
                raise UserError("That certificate no longer exists")
            clash = _clash_in(
                self._names_in_directory(directory_id), rec.name, file_id)
            if clash:
                return {"ok": False, "code": "name_exists",
                        "fileId": clash["id"], "name": clash["name"],
                        "error": "The destination folder already has “%s”. "
                                 "Rename one of them first." % clash["name"]}
            rec.write({"directory_id": directory_id})
            return {"ok": True}

        if action == "archive":
            # Soft-delete (project convention): archive, don't hard-unlink.
            file_id = int(params.get("fileId") or 0)
            if not file_id:
                raise UserError("fileId required")
            request.env["dms.file"].browse(file_id).write({"active": False})
            return {"ok": True}

        if action == "short_link":
            # Find-or-create the tracked short link for one certificate.
            file_id = int(params.get("fileId") or 0)
            if not file_id:
                raise UserError("fileId required")
            rec = request.env["dms.file"].browse(file_id)
            if not rec.exists():
                raise UserError("That certificate no longer exists")
            att_id = self._attachment_map([file_id]).get(file_id)
            if not att_id:
                raise UserError("That certificate has no stored PDF to link to")

            # The short link is meant to be handed to customers, so the target
            # has to be readable without an Odoo session. Uploads already set
            # this; older rows may predate that.
            att = request.env["ir.attachment"].browse(att_id)
            if not att.public:
                att.write({"public": True})

            existed = bool(request.env["link.tracker"].sudo().search_count(
                [("url", "=", self._content_url(att_id))]))
            link = self._mint_short_link(att_id, rec.name, rec.id)
            return {
                "ok": True,
                "shortUrl": link.short_url,
                "clicks": link.count,
                "existed": existed,
            }

        if action == "qr_code":
            # QR for the certificate's short link. mint_link_tracker_qr adds
            # qr_code/qr_code_filename to link.tracker as NON-STORED computes,
            # so they are rendered per read — fetched on demand for one row
            # rather than for every row in a folder listing.
            file_id = int(params.get("fileId") or 0)
            if not file_id:
                raise UserError("fileId required")
            rec = request.env["dms.file"].browse(file_id)
            if not rec.exists():
                raise UserError("That certificate no longer exists")
            att_id = self._attachment_map([file_id]).get(file_id)
            if not att_id:
                raise UserError("That certificate has no stored PDF to link to")
            link = self._mint_short_link(att_id, rec.name, rec.id)
            qr = link.qr_code
            if isinstance(qr, bytes):
                qr = qr.decode()
            return {
                "ok": True,
                "shortUrl": link.short_url,
                "qrBase64": qr or None,
                "filename": link.qr_code_filename or ("qr-%s.png" % link.code),
            }

        if action == "create_brand":
            name = _sanitize(params.get("name"))
            if not name:
                raise UserError("name required")
            # Same key as the files: a second "Stiiizy" next to "STIIIZY" splits
            # one brand's certificates across two folders that look identical.
            existing = request.env["dms.directory"].search_read(
                [("parent_id", "=", root)], ["id", "name"], limit=1000)
            same = _clash_in(existing, name)
            if same:
                return {"ok": True, "directoryId": same["id"],
                        "name": same["name"], "existed": True}
            rec = request.env["dms.directory"].create({
                "name": name, "parent_id": root,
                "group_ids": [(6, 0, [self._access_group_id()])],
            })
            return {"ok": True, "directoryId": rec.id}

        # --- access management (elevated, scoped to the COA group only) ------
        if action == "list_access":
            members = request.env["res.users"].sudo().browse(self._member_ids())
            return {"members": [
                {"id": u.id, "name": u.name, "login": u.login, "isYou": u.id == request.env.uid}
                for u in members]}

        if action == "search_users":
            q = str(params.get("query") or "").strip()
            if len(q) < 2:
                return {"users": []}
            member_ids = self._member_ids()
            found = request.env["res.users"].sudo().search_read(
                ["&", ("active", "=", True), ("share", "=", False),
                 "|", ("name", "ilike", q), ("login", "ilike", q)],
                ["id", "name", "login"], limit=10, order="name")
            return {"users": [u for u in found if u["id"] not in member_ids]}

        if action == "grant_access":
            target_id = int(params.get("userId") or 0)
            if not target_id:
                raise UserError("userId required")
            members = self._member_ids()
            if target_id in members:
                return {"ok": True, "already": True}
            target = request.env["res.users"].sudo().browse(target_id)
            if not target.exists() or not target.active:
                raise UserError("That user does not exist or is archived")
            if target.share:
                raise UserError("Portal/public users cannot be given COA access")
            request.env["dms.access.group"].sudo().browse(self._access_group_id()).write(
                {"explicit_user_ids": [(4, target_id)]})
            # Base DMS group so the Documents app is usable at all.
            target.write({"group_ids": [(4, request.env.ref("dms.group_dms_user").id)]})
            return {"ok": True, "name": target.name}

        if action == "revoke_access":
            target_id = int(params.get("userId") or 0)
            if not target_id:
                raise UserError("userId required")
            members = self._member_ids()
            if target_id not in members:
                return {"ok": True, "already": True}
            remaining = [m for m in members if m != target_id]
            if not remaining:
                raise UserError("Cannot remove the last person with COA access")
            grp = request.env["dms.access.group"].sudo().browse(self._access_group_id())
            # (3,id) removals silently no-op in Odoo 19 — set the full list.
            keep = [m for m in grp.explicit_user_ids.ids if m != target_id]
            grp.write({"explicit_user_ids": [(6, 0, keep)]})
            return {"ok": True, "selfRemoved": target_id == request.env.uid}

        raise UserError("Unknown action: %s" % action)
