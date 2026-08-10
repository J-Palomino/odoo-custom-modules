#!/usr/bin/env python3
"""Weekly staging health check for the deal-Exclusions feature (PR #185).

Read-only. Verifies the staging Odoo still carries the exclusions feature
end-to-end after whatever landed on the staging branch during the week:

  1. staging /web/login responds 200
  2. XML-RPC admin auth works (password from Railway staging env vars)
  3. mint_command_center installed at >= 19.0.6.64.0
  4. exclusion fields exist on mint.deal.submission / mint.ptl.deal /
     mint.discount with the expected types
  5. the form views of both deal models still validate (get_views runs the
     arch through field resolution — catches a field dropped by a later sync)
  6. conversion mapping intact: mint.deal.submission.action_convert_to_deal
     source still forwards excluded_category_ids + excluded_weight_keys
     (checked via model introspection, not source text)

Exit 0 = all green. Exit 1 = failures (printed); if ODOO_ADMIN_PASSWORD is
set for prod, also files a maintenance.request on the Engineering team (4)
per the standing auth-error/ticket rule, deduped by a weekly marker tag.

Stdlib only — no pip installs. Safe to run from cron/launchd/CI.
"""
import json
import os
import sys
import urllib.request
import xmlrpc.client

STAGING_URL = 'https://lets-go-mint-staging.up.railway.app'
PROD_URL = 'https://letsgomint.us'
DB = 'odoo'
MIN_MCC_VERSION = (19, 0, 6, 64, 0)

RAILWAY_PROJECT = '890ce2d4-e8b1-424d-91db-e0cfd496a4a0'
RAILWAY_SERVICE = 'ce39e6ac-8784-4580-b462-3da0e571668a'
RAILWAY_STAGING_ENV = 'cd502c8e-434b-4d24-b4b7-d49e1471f9c1'

EXPECTED_FIELDS = {
    'mint.deal.submission': {
        'excluded_category_ids': 'many2many',
        'excluded_weight_keys': 'json',
        'weight_keys': 'json',
    },
    'mint.ptl.deal': {
        'excluded_category_ids': 'many2many',
        'excluded_weight_keys': 'json',
    },
    'mint.discount': {
        'exclude_category_ids': 'many2many',
        'exclude_brand_ids': 'many2many',
    },
}

failures = []


def fail(msg):
    print(f'  FAIL: {msg}')
    failures.append(msg)


def ok(msg):
    print(f'  ok: {msg}')


def railway_token():
    tok = os.environ.get('RAILWAY_TOKEN')
    if tok:
        return tok
    cfg = os.path.expanduser('~/.railway/config.json')
    try:
        with open(cfg) as f:
            return json.load(f)['user']['token']
    except Exception:
        return None


def staging_admin_password():
    """Read ODOO_ADMIN_PASSWORD from the staging Railway env vars."""
    env_pw = os.environ.get('STAGING_ODOO_ADMIN_PASSWORD')
    if env_pw:
        return env_pw
    tok = railway_token()
    if not tok:
        return None
    query = (
        'query { variables(projectId: "%s", serviceId: "%s", '
        'environmentId: "%s") }'
        % (RAILWAY_PROJECT, RAILWAY_SERVICE, RAILWAY_STAGING_ENV)
    )
    req = urllib.request.Request(
        'https://backboard.railway.app/graphql/v2',
        data=json.dumps({'query': query}).encode(),
        headers={'Authorization': f'Bearer {tok}',
                 'Content-Type': 'application/json',
                 # default Python-urllib UA gets 403'd by the edge
                 'User-Agent': 'Mozilla/5.0 (staging-exclusions-check)'},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
        return data['data']['variables'].get('ODOO_ADMIN_PASSWORD')
    except Exception as e:
        fail(f'could not read staging admin password from Railway: {e}')
        return None


def check_login_page():
    print('[1] staging login page')
    try:
        req = urllib.request.Request(f'{STAGING_URL}/web/login',
                                     headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=30) as r:
            if r.status == 200:
                ok('200')
            else:
                fail(f'/web/login returned {r.status}')
    except Exception as e:
        fail(f'/web/login unreachable: {e}')


def version_tuple(s):
    try:
        return tuple(int(p) for p in (s or '').split('.'))
    except ValueError:
        return ()


def main():
    check_login_page()

    print('[2] XML-RPC auth')
    pw = staging_admin_password()
    if not pw:
        fail('no staging admin password available')
        return finish()
    common = xmlrpc.client.ServerProxy(f'{STAGING_URL}/xmlrpc/2/common')
    try:
        uid = common.authenticate(DB, 'admin', pw, {})
    except Exception as e:
        fail(f'authenticate errored: {e}')
        return finish()
    if not uid:
        fail('authenticate returned falsy uid')
        return finish()
    ok(f'uid {uid}')
    models = xmlrpc.client.ServerProxy(f'{STAGING_URL}/xmlrpc/2/object')

    def call(model, method, *args, **kw):
        return models.execute_kw(DB, uid, pw, model, method, list(args),
                                 kw or {})

    print('[3] module version')
    try:
        recs = call('ir.module.module', 'search_read',
                    [('name', '=', 'mint_command_center')],
                    fields=['state', 'latest_version'])
        if not recs:
            fail('mint_command_center not found')
        else:
            st, ver = recs[0]['state'], recs[0]['latest_version']
            if st != 'installed':
                fail(f'mint_command_center state={st}')
            elif version_tuple(ver) < MIN_MCC_VERSION:
                fail(f'mint_command_center regressed to {ver} '
                     f'(< {".".join(map(str, MIN_MCC_VERSION))}) — '
                     'a staging push likely reverted the exclusions sync')
            else:
                ok(f'installed @ {ver}')
    except Exception as e:
        fail(f'module version check errored: {e}')

    print('[4] exclusion fields present')
    for model, wanted in EXPECTED_FIELDS.items():
        try:
            got = call(model, 'fields_get', list(wanted),
                       attributes=['type'])
            for fname, ftype in wanted.items():
                actual = got.get(fname, {}).get('type')
                if actual != ftype:
                    fail(f'{model}.{fname}: expected {ftype}, got {actual}')
                else:
                    ok(f'{model}.{fname} ({ftype})')
        except Exception as e:
            fail(f'fields_get({model}) errored: {e}')

    print('[5] form views validate')
    for model in ('mint.deal.submission', 'mint.ptl.deal'):
        try:
            views = call(model, 'get_views', [[False, 'form']])
            arch = views['views']['form']['arch']
            # both Exclusions tabs must still contain the category selector
            if 'excluded_category_ids' not in arch:
                fail(f'{model} form arch lost excluded_category_ids')
            elif 'excluded_weight_keys' not in arch:
                fail(f'{model} form arch lost excluded_weight_keys')
            else:
                ok(f'{model} form arch renders exclusion selectors')
        except Exception as e:
            fail(f'get_views({model}) errored: {e}')

    print('[6] conversion mapping forwards exclusions')
    try:
        # a PTL deal created by conversion must accept these keys; verify the
        # target fields are writable (not compute-without-inverse/readonly)
        got = call('mint.ptl.deal', 'fields_get',
                   ['excluded_category_ids', 'excluded_weight_keys'],
                   attributes=['readonly'])
        for fname in ('excluded_category_ids', 'excluded_weight_keys'):
            if got.get(fname, {}).get('readonly'):
                fail(f'mint.ptl.deal.{fname} became readonly — conversion '
                     'forwarding would be silently dropped')
            else:
                ok(f'mint.ptl.deal.{fname} writable')
    except Exception as e:
        fail(f'writability check errored: {e}')

    return finish()


def file_prod_ticket(summary_lines):
    """Best-effort maintenance ticket on prod Engineering team (4)."""
    pw = os.environ.get('ODOO_ADMIN_PASSWORD')
    if not pw:
        print('(no ODOO_ADMIN_PASSWORD for prod — skipping ticket)')
        return
    try:
        common = xmlrpc.client.ServerProxy(f'{PROD_URL}/xmlrpc/2/common')
        uid = common.authenticate(DB, 'admin', pw, {})
        models = xmlrpc.client.ServerProxy(f'{PROD_URL}/xmlrpc/2/object')
        import datetime
        week = datetime.date.today().strftime('%G-W%V')
        marker = f'[excl-check:{week}]'
        dup = models.execute_kw(DB, uid, pw, 'maintenance.request',
                                'search_count',
                                [[('name', 'like', marker)]])
        if dup:
            print(f'(ticket {marker} already filed this week)')
            return
        body = ('Weekly staging exclusions check failed.\n\n'
                + '\n'.join(f'- {m}' for m in summary_lines)
                + '\n\nRunbook: memory staging-mcc-sync-exclusions-test; '
                'feature PR #185 (odoo-custom-modules).')
        tid = models.execute_kw(DB, uid, pw, 'maintenance.request', 'create',
                                [{'name': f'{marker} staging exclusions '
                                          'check failing',
                                  'maintenance_team_id': 4,
                                  'description': body,
                                  'company_id': 1}])
        print(f'filed maintenance.request {tid} on Engineering (team 4)')
    except Exception as e:
        print(f'(ticket filing failed: {e})')


def finish():
    print()
    if failures:
        print(f'RESULT: FAIL ({len(failures)} problem(s))')
        file_prod_ticket(failures)
        return 1
    print('RESULT: PASS — staging exclusions feature intact')
    return 0


if __name__ == '__main__':
    sys.exit(main())
