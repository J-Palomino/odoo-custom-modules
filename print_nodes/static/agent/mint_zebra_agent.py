#!/usr/bin/env python3
"""
Mint Zebra Print Agent — a tiny localhost print helper for the POS PWA.

Mimics the PrintNode "local client" model without the SaaS: the POS (a web
app) POSTs raw ZPL to this agent on 127.0.0.1, and the agent prints it to the
OS-installed Zebra (ZD410) through the normal printer driver.

  - Windows: raw via the spooler (ctypes/winspool, RAW datatype) — NO Zadig /
    WinUSB driver swap, NO pywin32 needed.
  - macOS:   raw via the CUPS USB backend (CUPS filters `lp -o raw` on recent
    macOS, so we write straight to the device).
  - Linux:   raw via `lp -o raw`.

Endpoints (CORS + Private Network Access enabled so an HTTPS POS page can
call http://127.0.0.1):
  GET  /status            -> {ok, os, default_printer, port}
  GET  /printers          -> {printers: [...]}
  POST /print             -> body is raw ZPL (text/plain) or JSON {zpl, printer}

Run:
  python3 mint_zebra_agent.py                 # port 17777, default printer
  MINT_AGENT_PORT=17777 MINT_AGENT_TOKEN=secret MINT_AGENT_PRINTER="ZD410" \\
      python3 mint_zebra_agent.py

Security: binds 127.0.0.1 only. Set MINT_AGENT_TOKEN to require an
X-Agent-Token header (the POS sends it). Set MINT_AGENT_ORIGIN to restrict
which web origin may call it (default '*').
"""
import base64
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _load_config():
    """Populate os.environ from a KEY=VALUE config file (env still wins).

    Default path: mint_print_agent.conf next to this script. Override with
    MINT_AGENT_CONFIG. Lets the install-once service run with no env vars set.
    """
    path = os.environ.get('MINT_AGENT_CONFIG') or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'mint_print_agent.conf')
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    os.environ.setdefault(k.strip(), v.strip())
    except OSError:
        pass


_load_config()

PORT = int(os.environ.get('MINT_AGENT_PORT', '17777'))
TOKEN = os.environ.get('MINT_AGENT_TOKEN', '')
ORIGIN = os.environ.get('MINT_AGENT_ORIGIN', '*')
PRINTER = os.environ.get('MINT_AGENT_PRINTER', '')  # blank => OS default
OSNAME = platform.system()  # 'Windows' | 'Darwin' | 'Linux'

# Node mode (store queue): set both to enable polling Odoo for store-wide jobs.
ODOO_URL = os.environ.get('MINT_ODOO_URL', '').rstrip('/')
NODE_TOKEN = os.environ.get('MINT_NODE_TOKEN', '')
POLL_SECS = float(os.environ.get('MINT_POLL_SECS', '3'))


# ── platform printing ────────────────────────────────────────────────
def _win_default_printer():
    import ctypes
    from ctypes import wintypes
    buf = ctypes.create_unicode_buffer(256)
    size = wintypes.DWORD(256)
    # The spooler API lives in winspool.drv; ctypes.windll.winspool looks for
    # winspool.dll, which is not present on modern Windows and raises
    # "Could not find module 'winspool'". Load winspool.drv explicitly.
    ctypes.WinDLL('winspool.drv').GetDefaultPrinterW(buf, ctypes.byref(size))
    return buf.value


def _win_list_printers():
    try:
        out = subprocess.run(
            ['powershell', '-NoProfile', '-Command',
             'Get-Printer | Select-Object -ExpandProperty Name'],
            capture_output=True, text=True, timeout=10)
        return [p.strip() for p in out.stdout.splitlines() if p.strip()]
    except Exception:
        d = _win_default_printer()
        return [d] if d else []


def _win_print_raw(printer, data):
    import ctypes
    from ctypes import wintypes
    winspool = ctypes.WinDLL('winspool.drv')  # not windll.winspool (winspool.dll is absent on modern Windows)
    hPrinter = wintypes.HANDLE()
    if not winspool.OpenPrinterW(printer, ctypes.byref(hPrinter), None):
        raise OSError('OpenPrinter failed for %r' % printer)
    try:
        class DOC_INFO_1(ctypes.Structure):
            _fields_ = [('pDocName', wintypes.LPWSTR),
                        ('pOutputFile', wintypes.LPWSTR),
                        ('pDatatype', wintypes.LPWSTR)]
        doc = DOC_INFO_1('Mint Zebra Label', None, 'RAW')
        job = winspool.StartDocPrinterW(hPrinter, 1, ctypes.byref(doc))
        if not job:
            raise OSError('StartDocPrinter failed')
        winspool.StartPagePrinter(hPrinter)
        written = wintypes.DWORD(0)
        buf = ctypes.create_string_buffer(data, len(data))
        if not winspool.WritePrinter(hPrinter, buf, len(data), ctypes.byref(written)):
            raise OSError('WritePrinter failed')
        winspool.EndPagePrinter(hPrinter)
        winspool.EndDocPrinter(hPrinter)
        return written.value
    finally:
        winspool.ClosePrinter(hPrinter)


def _cups_default_printer():
    try:
        out = subprocess.run(['lpstat', '-d'], capture_output=True, text=True, timeout=8)
        # "system default destination: NAME"
        if ':' in out.stdout:
            return out.stdout.split(':', 1)[1].strip()
    except Exception:
        pass
    return ''


def _cups_list_printers():
    try:
        out = subprocess.run(['lpstat', '-p'], capture_output=True, text=True, timeout=8)
        names = []
        for ln in out.stdout.splitlines():
            if ln.startswith('printer '):
                names.append(ln.split()[1])
        return names
    except Exception:
        return []


def _cups_device_uri(printer):
    out = subprocess.run(['lpstat', '-v', printer], capture_output=True, text=True, timeout=8)
    # "device for NAME: usb://..."
    if ':' in out.stdout:
        return out.stdout.split(':', 1)[1].strip()
    return ''


def _darwin_print_raw(printer, data):
    """Write straight to the device via the CUPS USB backend.

    `lp -o raw` is filtered on recent macOS, so we resolve the printer's
    device-uri and hand the bytes to the usb backend unfiltered.
    """
    uri = _cups_device_uri(printer)
    if uri.startswith('usb://'):
        env = dict(os.environ, DEVICE_URI=uri)
        backend = '/usr/libexec/cups/backend/usb'
        p = subprocess.run(
            [backend, '101', os.environ.get('USER', 'pos'), 'ZebraLabel', '1', ''],
            input=data, env=env, capture_output=True, timeout=30)
        if p.returncode != 0:
            raise OSError('usb backend rc=%s %s' % (p.returncode, p.stderr[:200]))
        return len(data)
    # networked / non-usb: fall back to lp raw
    return _lp_print_raw(printer, data)


def _lp_print_raw(printer, data):
    cmd = ['lp', '-o', 'raw']
    if printer:
        cmd += ['-d', printer]
    p = subprocess.run(cmd, input=data, capture_output=True, timeout=30)
    if p.returncode != 0:
        raise OSError('lp rc=%s %s' % (p.returncode, p.stderr[:200]))
    return len(data)


def default_printer():
    if OSNAME == 'Windows':
        return _win_default_printer()
    return _cups_default_printer()


def list_printers():
    if OSNAME == 'Windows':
        return _win_list_printers()
    return _cups_list_printers()


def print_raw(printer, data):
    printer = printer or PRINTER or default_printer()
    if not printer:
        raise OSError('no printer specified and no OS default printer')
    if OSNAME == 'Windows':
        return _win_print_raw(printer, data), printer
    if OSNAME == 'Darwin':
        return _darwin_print_raw(printer, data), printer
    return _lp_print_raw(printer, data), printer


# ── PDF printing (rendered by the OS driver — works on non-Zebra printers) ──
def _cups_print_pdf(printer, path):
    cmd = ['lp']                      # NO -o raw -> CUPS renders the PDF
    if printer:
        cmd += ['-d', printer]
    cmd += [path]
    p = subprocess.run(cmd, capture_output=True, timeout=120)
    if p.returncode != 0:
        raise OSError('lp pdf rc=%s %s' % (p.returncode, p.stderr[:200]))


def _win_print_pdf(printer, path):
    # Windows can't render PDF natively; use GhostScript or SumatraPDF.
    gs = shutil.which('gswin64c') or shutil.which('gswin32c') or shutil.which('gs')
    if gs:
        p = subprocess.run(
            [gs, '-dPrinted', '-dBATCH', '-dNOPAUSE', '-q', '-dNOSAFER',
             '-sDEVICE=mswinpr2', '-sOutputFile=%%printer%%%s' % printer, path],
            capture_output=True, timeout=120)
        if p.returncode != 0:
            raise OSError('ghostscript rc=%s %s' % (p.returncode, p.stderr[:200]))
        return
    # SumatraPDF: on PATH, or bundled next to the agent. The register installer
    # drops SumatraPDF.exe in the agent's own ProgramData dir, which is NOT on
    # PATH, so look there too.
    here = os.path.dirname(os.path.abspath(__file__))
    sumatra = (shutil.which('SumatraPDF') or shutil.which('SumatraPDF.exe') or
               next((p for p in (os.path.join(here, 'SumatraPDF.exe'),
                                 r'C:\ProgramData\MintPrintAgent\SumatraPDF.exe')
                     if os.path.exists(p)), None))
    if sumatra:
        p = subprocess.run([sumatra, '-print-to', printer, '-silent',
                            '-exit-when-done', path],
                           capture_output=True, timeout=120)
        if p.returncode != 0:
            raise OSError('SumatraPDF rc=%s' % p.returncode)
        return
    raise OSError('Windows PDF printing needs GhostScript (gswin64c) or SumatraPDF installed')


def print_pdf(printer, pdf_bytes):
    printer = printer or PRINTER or default_printer()
    if not printer:
        raise OSError('no printer specified and no OS default printer')
    fd, path = tempfile.mkstemp(suffix='.pdf')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(pdf_bytes)
        if OSNAME == 'Windows':
            _win_print_pdf(printer, path)
        else:
            _cups_print_pdf(printer, path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return len(pdf_bytes), printer


def print_doc(job):
    """Print a poll job by its doc_type. Returns (bytes, printer)."""
    printer = job.get('printer') or None
    doc_type = job.get('doc_type')
    if doc_type == 'pdf':
        return print_pdf(printer, base64.b64decode(job.get('pdf') or ''))
    if doc_type == 'escpos':
        # Raw ESC/POS bytes for a Star/Epson receipt printer, carried base64 in
        # the same field as PDFs (its bytes include NUL, so it cannot ride in the
        # zpl text field). Send verbatim to the printer, no rendering.
        return print_raw(printer, base64.b64decode(job.get('pdf') or ''))
    return print_raw(printer, (job.get('zpl') or '').encode('utf-8'))


# ── HTTP server ──────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', ORIGIN)
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Agent-Token')
        self.send_header('Access-Control-Allow-Private-Network', 'true')

    def _json(self, code, obj):
        body = json.dumps(obj).encode('utf-8')
        self.send_response(code)
        self._cors()
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self):
        return not TOKEN or self.headers.get('X-Agent-Token', '') == TOKEN

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path.startswith('/status'):
            self._json(200, {'ok': True, 'os': OSNAME, 'port': PORT,
                             'default_printer': default_printer(),
                             'agent': 'mint-zebra-agent'})
        elif self.path.startswith('/printers'):
            self._json(200, {'printers': list_printers(),
                             'default': default_printer()})
        else:
            self._json(404, {'error': 'not found'})

    def do_POST(self):
        if not self.path.startswith('/print'):
            return self._json(404, {'error': 'not found'})
        if not self._auth_ok():
            return self._json(401, {'error': 'bad token'})
        length = int(self.headers.get('Content-Length', '0') or 0)
        raw = self.rfile.read(length) if length else b''
        printer = None
        ctype = (self.headers.get('Content-Type') or '').lower()
        if 'application/json' in ctype:
            try:
                payload = json.loads(raw.decode('utf-8'))
                zpl = (payload.get('zpl') or '').encode('utf-8')
                printer = payload.get('printer')
            except Exception as exc:
                return self._json(400, {'error': 'bad json: %s' % exc})
        else:
            zpl = raw
        if not zpl:
            return self._json(400, {'error': 'empty ZPL'})
        try:
            written, used = print_raw(printer, zpl)
            self._json(200, {'ok': True, 'bytes': written, 'printer': used})
        except Exception as exc:
            self._json(500, {'ok': False, 'error': str(exc)})

    def log_message(self, fmt, *args):  # quieter logging
        sys.stderr.write('[mint-zebra-agent] ' + (fmt % args) + '\n')


# ── node mode: register + poll the Odoo store queue ──────────────────
def _odoo_post(path, obj):
    req = urllib.request.Request(
        ODOO_URL + path, data=json.dumps(obj).encode('utf-8'),
        headers={'Content-Type': 'application/json',
                 'User-Agent': 'MintPrintAgent/1.0'}, method='POST')
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read() or b'{}')


def node_register():
    printers = [{'system_name': n, 'name': n} for n in list_printers()]
    try:
        res = _odoo_post('/mint/print/register',
                         {'token': NODE_TOKEN, 'hostname': platform.node(),
                          'printers': printers})
        print('Registered node "%s" (%s) with %d printer(s)'
              % (res.get('node'), res.get('company'), len(res.get('printers', []))))
    except Exception as exc:
        print('register failed: %s' % exc)


def node_poll_loop():
    print('Node mode: polling %s every %ss' % (ODOO_URL, POLL_SECS))
    node_register()
    last_register = time.time()
    while True:
        try:
            res = _odoo_post('/mint/print/poll', {'token': NODE_TOKEN})
            for job in res.get('jobs', []):
                jid = job.get('job_id')
                try:
                    written, used = print_doc(job)
                    _odoo_post('/mint/print/complete',
                               {'token': NODE_TOKEN, 'job_id': jid,
                                'ok': True, 'bytes': written})
                    print('printed job %s (%s) -> %s (%d bytes)'
                          % (jid, job.get('doc_type', 'zpl'), used, written))
                except Exception as exc:
                    _odoo_post('/mint/print/complete',
                               {'token': NODE_TOKEN, 'job_id': jid,
                                'ok': False, 'error': str(exc)})
                    print('job %s failed: %s' % (jid, exc))
        except Exception as exc:
            print('poll error: %s' % exc)
        # refresh registration (printer list) every ~5 min
        if time.time() - last_register > 300:
            node_register()
            last_register = time.time()
        time.sleep(POLL_SECS)


def main():
    if ODOO_URL and NODE_TOKEN:
        threading.Thread(target=node_poll_loop, daemon=True).start()
    srv = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    print('Mint Zebra Print Agent on http://127.0.0.1:%d (%s)' % (PORT, OSNAME))
    print('Default printer: %r' % (default_printer() or '(none)'))
    if TOKEN:
        print('Token auth: ON')
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == '__main__':
    main()
