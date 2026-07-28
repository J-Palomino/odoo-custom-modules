"""COA Document Store — staff admin panel, served natively by Odoo.

Ported from the standalone Astro/Cloudflare panel. Because it runs inside Odoo
under ``auth='user'``, the visitor authenticates with their normal Odoo session
(including "Sign in with Google" on the Odoo login page), every operation runs
as ``request.env.user`` so Odoo's own DMS access rules decide what they may
touch, and edits are audited to them. Only the narrow COA access-group
membership writes are elevated with ``sudo()`` — and only after proving the
caller is themselves a COA member.
"""

import base64
import os

from odoo import http
from odoo.exceptions import AccessError, UserError
from odoo.http import request

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

        if action == "upload":
            directory_id = int(params.get("directoryId") or 0)
            name = _sanitize(params.get("name"))
            content_b64 = params.get("contentBase64") or ""
            if not directory_id or not name or not content_b64:
                raise UserError("directoryId, name and contentBase64 required")
            rec = request.env["dms.file"].create({
                "name": name, "directory_id": directory_id, "content": content_b64,
            })
            att = self._attachment_map([rec.id]).get(rec.id)
            if att:
                # Make the content public so /web/content and /coa can serve it.
                request.env["ir.attachment"].browse(att).write({"public": True})
            return {"ok": True, "fileId": rec.id, "attachmentId": att}

        if action == "rename":
            file_id = int(params.get("fileId") or 0)
            name = _sanitize(params.get("name"))
            if not file_id or not name:
                raise UserError("fileId and name required")
            request.env["dms.file"].browse(file_id).write({"name": name})
            return {"ok": True}

        if action == "move":
            file_id = int(params.get("fileId") or 0)
            directory_id = int(params.get("directoryId") or 0)
            if not file_id or not directory_id:
                raise UserError("fileId and directoryId required")
            request.env["dms.file"].browse(file_id).write({"directory_id": directory_id})
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

            target = self._content_url(att_id)
            tracker = request.env["link.tracker"].sudo()
            existing = tracker.search([("url", "=", target)], limit=1)
            if existing:
                link = existing
            else:
                # NB: passing `code` to create() is silently ignored (it is a
                # non-stored compute whose inverse doesn't run on create), so we
                # take Odoo's generated code rather than pretending to set one.
                link = tracker.create({"url": target, "title": rec.name})
            return {
                "ok": True,
                "shortUrl": link.short_url,
                "clicks": link.count,
                "existed": bool(existing),
            }

        if action == "create_brand":
            name = _sanitize(params.get("name"))
            if not name:
                raise UserError("name required")
            existing = request.env["dms.directory"].search(
                [("parent_id", "=", root), ("name", "=", name)], limit=1)
            if existing:
                return {"ok": True, "directoryId": existing.id, "existed": True}
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
