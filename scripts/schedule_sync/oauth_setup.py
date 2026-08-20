#!/usr/bin/env python3
"""One-time: mint a Drive-scoped refresh token using the Google OAuth client
already configured in Odoo, and store it back in Odoo for the sync to use.

Reuses `google_calendar_client_id` / `google_calendar_client_secret` from
`ir.config_parameter` — the OAuth client the Daisy/Odoo stack already owns —
rather than introducing a new credential. The resulting refresh token is stored
as `schedule_sync.google_refresh_token`, so unattended runs never need a browser
again.

    python3 oauth_setup.py              # run the consent flow
    python3 oauth_setup.py --check      # show what is stored, mint a test token
    python3 oauth_setup.py --port 8765  # loopback port (must be registered)

If Google rejects the callback with `redirect_uri_mismatch`, add the exact
loopback URI this script prints to the OAuth client's authorised redirect URIs
in the Google Cloud console (project `letsgomint-us`).
"""

import argparse
import http.server
import os
import socket
import sys
import threading
import urllib.parse
import webbrowser

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from odoo import Odoo

AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
USERINFO = 'https://www.googleapis.com/oauth2/v3/userinfo'

CLIENT_ID_KEY = 'google_calendar_client_id'
CLIENT_SECRET_KEY = 'google_calendar_client_secret'
REFRESH_KEY = 'schedule_sync.google_refresh_token'
ACCOUNT_KEY = 'schedule_sync.google_account'

SCOPES = ['https://www.googleapis.com/auth/drive.readonly']


def get_param(odoo, key):
    r = odoo.execute('ir.config_parameter', 'search_read', [['key', '=', key]],
                     fields=['value'], limit=1)
    return r[0]['value'] if r else None


def set_param(odoo, key, value):
    existing = odoo.execute('ir.config_parameter', 'search_read',
                            [['key', '=', key]], fields=['id'], limit=1)
    if existing:
        odoo.execute('ir.config_parameter', 'write', [existing[0]['id']],
                     {'value': value})
    else:
        odoo.execute('ir.config_parameter', 'create',
                     {'key': key, 'value': value})


class _Catcher(http.server.BaseHTTPRequestHandler):
    code = None
    error = None

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _Catcher.code = (q.get('code') or [None])[0]
        _Catcher.error = (q.get('error') or [None])[0]
        body = (b'<h2>Authorised.</h2><p>You can close this tab.</p>'
                if _Catcher.code else
                b'<h2>Authorisation failed.</h2><p>Check the terminal.</p>')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def port_free(port):
    with socket.socket() as s:
        try:
            s.bind(('127.0.0.1', port))
            return True
        except OSError:
            return False


ODOO_REDIRECT = 'https://letsgomint.us/google_account/authentication'


def run_flow(odoo, port, no_browser=False, manual=False, redirect_uri=None):
    client_id = get_param(odoo, CLIENT_ID_KEY)
    client_secret = get_param(odoo, CLIENT_SECRET_KEY)
    if not client_id or not client_secret:
        raise SystemExit(f'missing {CLIENT_ID_KEY}/{CLIENT_SECRET_KEY} in Odoo')

    if manual:
        return _manual_flow(odoo, client_id, client_secret,
                            redirect_uri or ODOO_REDIRECT, no_browser)

    if not port_free(port):
        raise SystemExit(f'port {port} is in use — pass --port')

    redirect_uri = redirect_uri or f'http://localhost:{port}/'
    params = {
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'scope': ' '.join(SCOPES),
        'access_type': 'offline',
        'prompt': 'consent',
        'include_granted_scopes': 'true',
    }
    url = AUTH_URL + '?' + urllib.parse.urlencode(params)

    srv = http.server.HTTPServer(('127.0.0.1', port), _Catcher)
    threading.Thread(target=srv.handle_request, daemon=True).start()

    print(f'redirect_uri : {redirect_uri}')
    print('\nOpen this URL and approve access with the Google account that can '
          'see the schedule sheets:\n')
    print(url + '\n')
    if not no_browser:
        webbrowser.open(url)

    print('waiting for the callback…')
    for _ in range(300):
        if _Catcher.code or _Catcher.error:
            break
        threading.Event().wait(1)
    srv.server_close()

    if _Catcher.error:
        raise SystemExit(f'authorisation failed: {_Catcher.error}')
    if not _Catcher.code:
        raise SystemExit('timed out waiting for the callback')

    tok = requests.post(TOKEN_URL, timeout=60, data={
        'code': _Catcher.code,
        'client_id': client_id,
        'client_secret': client_secret,
        'redirect_uri': redirect_uri,
        'grant_type': 'authorization_code',
    })
    if tok.status_code != 200:
        raise SystemExit(f'token exchange failed: HTTP {tok.status_code} '
                         f'{tok.text[:300]}')
    payload = tok.json()
    refresh = payload.get('refresh_token')
    if not refresh:
        raise SystemExit('no refresh_token returned — re-run; Google only '
                         'issues one with prompt=consent + access_type=offline')

    who = requests.get(USERINFO, timeout=30, headers={
        'Authorization': f"Bearer {payload['access_token']}"}).json()
    email = who.get('email', '(unknown)')

    set_param(odoo, REFRESH_KEY, refresh)
    set_param(odoo, ACCOUNT_KEY, email)
    print(f'\nstored refresh token for {email}')
    print(f'  {REFRESH_KEY}')
    print(f'  {ACCOUNT_KEY}')
    print('\nsync.py will now authenticate on its own.')


def _exchange(odoo, client_id, client_secret, code, redirect_uri):
    tok = requests.post(TOKEN_URL, timeout=60, data={
        'code': code,
        'client_id': client_id,
        'client_secret': client_secret,
        'redirect_uri': redirect_uri,
        'grant_type': 'authorization_code',
    })
    if tok.status_code != 200:
        raise SystemExit(f'token exchange failed: HTTP {tok.status_code} '
                         f'{tok.text[:300]}')
    payload = tok.json()
    refresh = payload.get('refresh_token')
    if not refresh:
        raise SystemExit('no refresh_token returned — re-run the flow')
    who = requests.get(USERINFO, timeout=30, headers={
        'Authorization': f"Bearer {payload['access_token']}"}).json()
    email = who.get('email', '(unknown)')
    set_param(odoo, REFRESH_KEY, refresh)
    set_param(odoo, ACCOUNT_KEY, email)
    print(f'\nstored refresh token for {email}')
    print('sync.py will now authenticate on its own.')


def _manual_flow(odoo, client_id, client_secret, redirect_uri, no_browser):
    """Consent against a redirect URI Odoo already has registered, then paste
    the `code` out of the address bar. Avoids depending on a loopback URI being
    registered on the OAuth client."""
    params = {
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'scope': ' '.join(SCOPES),
        'access_type': 'offline',
        'prompt': 'consent',
    }
    url = AUTH_URL + '?' + urllib.parse.urlencode(params)
    print(f'redirect_uri : {redirect_uri}\n')
    print('1. Open this URL and approve access with the Google account that '
          'can see the schedule sheets:\n')
    print(url + '\n')
    print('2. You will land on a letsgomint.us page that may show an error — '
          'that is fine.\n   Copy the `code=` value out of the address bar.\n')
    if not no_browser:
        webbrowser.open(url)
    code = input('paste code here: ').strip()
    if not code:
        raise SystemExit('no code supplied')
    if 'code=' in code:  # tolerate a pasted full URL
        code = urllib.parse.parse_qs(
            urllib.parse.urlparse(code).query).get('code', [code])[0]
    _exchange(odoo, client_id, client_secret, code, redirect_uri)


def check(odoo):
    client_id = get_param(odoo, CLIENT_ID_KEY)
    refresh = get_param(odoo, REFRESH_KEY)
    account = get_param(odoo, ACCOUNT_KEY)
    print(f'client_id     : {"set" if client_id else "MISSING"}')
    print(f'refresh token : {"set" if refresh else "MISSING"}')
    print(f'account       : {account or "-"}')
    if not (client_id and refresh):
        return 1
    tok = requests.post(TOKEN_URL, timeout=60, data={
        'client_id': client_id,
        'client_secret': get_param(odoo, CLIENT_SECRET_KEY),
        'refresh_token': refresh,
        'grant_type': 'refresh_token',
    })
    print(f'refresh works : {tok.status_code == 200}'
          f'{"" if tok.status_code == 200 else " -> " + tok.text[:200]}')
    return 0 if tok.status_code == 200 else 1


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--port', type=int, default=8765)
    p.add_argument('--check', action='store_true')
    p.add_argument('--no-browser', action='store_true')
    p.add_argument('--manual', action='store_true',
                   help='paste the code instead of using a loopback listener')
    p.add_argument('--redirect-uri', help='override the redirect URI')
    a = p.parse_args()
    odoo = Odoo()
    if a.check:
        return check(odoo)
    run_flow(odoo, a.port, a.no_browser, manual=a.manual,
             redirect_uri=a.redirect_uri)
    return 0


if __name__ == '__main__':
    sys.exit(main())
