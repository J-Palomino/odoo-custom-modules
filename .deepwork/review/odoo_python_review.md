# Odoo Custom Module Review

Review this Odoo Python file (or XML view / `__manifest__.py`) for the project-specific gotchas listed below. This is **odoo-custom-modules** — the canonical Odoo 19 modules repo that production Odoo builds from (`main` branch → Railway "Lets Go Mint"). It runs on **Odoo 19** with a multi-company hierarchy ("Mint Dispensaries" parent + 32+ child store companies).

## Hard rules — flag if violated

### Multi-company constraints
- Records that link to other records must respect `_check_company` / `_check_company_auto`. Cross-company FKs without an explicit company override will raise at write time.
- `sudo()` does NOT bypass SQL constraints or `_check_company_auto`. If a record creation/write needs to span companies, set `company_id` explicitly or use `with_company(...)` rather than relying on `sudo()`.
- Flag any new `create()` / `write()` that sets `company_id` to a hard-coded id without justification.

### Odoo 19 breaking changes
- **Search views** no longer support a `<group>` wrapper inside `<search>`. Flag any `<group>` directly under `<search>`.
- Product type `'product'` was removed. Use `is_storable=True` on a `consu` type instead. Flag any `type='product'` reference.
- On `event.event`, `website_description` was renamed to `description`. Flag any reference to `website_description`.
- On `res.users`, `groups_id` was renamed to `group_ids`. Flag any `groups_id` reference (write or domain). Same for menu/group `group_ids`.
- On groups, `category_id` was removed. Flag any reference.

### Archive over delete
- Prefer `action_archive()` / setting `active = False` over `unlink()` for records that may have FK references (orders, partners, products, companies). `unlink()` on referenced records throws FK errors and the prior incident pattern is to silently catch and lose data.
- Flag new `unlink()` calls on standard models without justification.

### REST controllers
- Any `@http.route(... type='json'|'http')` that does writes must enforce `auth='user'` (or stricter). Public write endpoints are a BLOCK unless explicitly justified.
- CSRF must be considered for `type='http'` POST routes — Odoo defaults to CSRF protection; explicit `csrf=False` requires justification.
- `sudo()` use should be justified. A single module- or class-level comment ("sudo() required throughout: auth='none' routes have no session user") covers all sudo() calls in that scope — do not flag individual sudo() calls when a scope-level comment already explains the pattern. Only flag sudo() when there is no explanation anywhere in the surrounding scope, or when sudo() is used in a context that does have a session user.

### Manifest sanity (`__manifest__.py`)
- Bumped `version` if model/view changes are present.
- New `data` files are listed in the manifest (a new `views/*.xml` or `security/*.csv` not in `data` will silently not load).
- New `depends` entries are correct (don't add unused deps).
- New modules must ship in the Dockerfile COPY list — a module absent from the `Dockerfile` `COPY` block deploys nothing. Flag a new top-level module dir whose name is not added to the Dockerfile.

### message_post / RPC safety
- Known prod bug: RPC `message_post` with HTML or list-bearing kwargs crashes `TypeError: unhashable type: 'list'`. Prefer plain-text `body=` chatter; flag `message_post` calls that pass HTML built from list comprehensions or `attachment_ids` as bare lists without justification.

## Warning-level checks
- Long-running computations in compute methods without `@api.depends(...)` will silently re-trigger. Flag missing or incorrect `@api.depends`.
- `env['ir.cron']` records should not be created in code without idempotency guards (look up by `cron_name` or use a unique XML id).
- Raw SQL via `self.env.cr.execute(...)` should use parameterized queries (never f-string interpolation of user/record data) and, for DDL like `CREATE INDEX`, be idempotent (`IF NOT EXISTS`).

## Output
- List each finding with severity (BLOCK / WARN / INFO), file:line, what's wrong, and the minimum fix.
- If the file is clean, say so in one line.
