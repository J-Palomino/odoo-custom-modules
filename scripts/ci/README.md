# CI checks

`check_modules.py` is the repo's merge gate. Stdlib only, no Odoo, no database —
it runs in seconds on a bare runner.

```bash
python3 scripts/ci/check_modules.py      # what CI runs
python3 scripts/ci/check_modules.py --update   # re-record the ratchet baseline
```

Every rule exists because that exact mistake has shipped from this repo before.

## Hard checks — must stay at zero

| Check | Why |
|---|---|
| `python-syntax` | A syntax error reaches the image and the module fails to load. |
| `xml-wellformed` | Malformed XML aborts the whole `-u` and rolls the upgrade back. |
| `manifest-parses` | Odoo literal-evals `__manifest__.py`; it must be a dict. |
| `manifest-data-files` | A `data:` entry pointing at a missing file kills the install. |
| `manifest-asset-files` | A dangling asset path fails at bundle-build time, in the browser, long after merge. |
| `renamed-fields` | Odoo 19 renames (`groups_id` → `group_ids`, `attrs=` removed). |
| `migration-dirs` | A migration dir that matches no manifest version **silently never runs** — a no-op that looks exactly like success. |

## Ratchet checks — may shrink, never grow

Pre-existing violations are recorded in `ci-baseline.json`. You are not asked to
fix them; you are blocked from adding more. Same pattern as the storefront
repo's `ui-boundary` and typecheck ratchets.

| Check | Baseline | Why |
|---|---|---|
| `group-in-search` | 10 | `<group>` inside a `<search>` view is **fatal on Odoo 19** — it invalidates the view, which rolls back the ENTIRE module upgrade behind a green deploy. This rolled `mint_api_v2` back twice. Put group-by filters directly in `<search>`. |
| `sql-constraints` | 18 | `_sql_constraints` is **silently ignored** on Odoo 19 — the constraint stops existing at the DB level with no error. Use `models.Constraint`. |
| `dockerfile-copy` | 15 | A module with no `COPY` line never reaches `/opt/extra-addons`, so Odoo cannot see or install it regardless of what is committed. |

All current `group-in-search` and most `sql-constraints` violations are in
vendored OCA modules; `dockerfile-copy` is mostly OCA modules that are
deliberately not deployed (**except `mint_tv`, which is ours and genuinely
cannot be installed today**).

### I hit a ratchet failure

Fix the thing you added — that is the point. Only run `--update` when you have
genuinely *reduced* a count, and say so in the commit message.
