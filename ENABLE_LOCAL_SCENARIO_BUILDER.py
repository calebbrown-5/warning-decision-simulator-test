#!/usr/bin/env python3
"""Enable one local-only HTML route in the existing WDS scenario_backend.py.

This does not change any archive, discovery, preparation or warning endpoints.
Runs once on the ORIGINAL LOCAL FACILITATOR copy containing scenario_backend.py.
"""
from pathlib import Path
import ast
import os
import secrets
import shutil
import sys

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / 'scenario_backend.py'
PAGE = ROOT / 'scenario_builder.html'
MARK = "if urlsplit(self.path).path=='/api/scenario/catalog':"
ROUTE = '''if urlsplit(self.path).path=='/scenario_builder.html':
            # WDS 3.35.0.1: UI-only route; same loopback host guard as the APIs.
            if not self._api_host_ok():
                return self._json(403, {'error':'Local training builder only'})
            page = ROOT / 'scenario_builder.html'
            try:
                content = page.read_bytes()
            except OSError:
                return self.send_error(404, 'Local scenario builder page missing')
            self.send_response(200)
            self.send_header('Content-Type','text/html; charset=utf-8')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Cache-Control','no-store')
            self.send_header('Content-Length',str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        '''

def stop(msg):
    print('STOPPED WITHOUT CHANGES:', msg)
    raise SystemExit(2)

if not BACKEND.is_file(): stop('scenario_backend.py not in this folder. Run this on the original local facilitator simulator copy, not a frontend-only folder.')
if not PAGE.is_file(): stop('scenario_builder.html is not in this folder. Install the frontend page first.')
src = BACKEND.read_text(encoding='utf-8')
if "if urlsplit(self.path).path=='/scenario_builder.html':" in src:
    print('ALREADY ENABLED: local Scenario Builder route exists. No changes.')
    raise SystemExit(0)
if src.count(MARK) != 1 or 'class ScenarioHandler(SafeHandler):' not in src or 'def _api_host_ok(self):' not in src:
    stop('Backend source layout differs from the reviewed version. Do not force-insert the route; share this file for inspection.')
out = src.replace(MARK, ROUTE + MARK)
try: ast.parse(out)
except SyntaxError as exc:stop('Patched backend failed Python syntax validation: '+str(exc))
backup=BACKEND.with_name('scenario_backend.py.backup-v33501-'+secrets.token_hex(4))
shutil.copy2(BACKEND,backup)
staged=BACKEND.with_name('.scenario_backend.v33501.tmp')
try:
    staged.write_text(out,encoding='utf-8')
    ast.parse(staged.read_text(encoding='utf-8'))
    os.replace(staged,BACKEND)
except Exception as exc:
    staged.unlink(missing_ok=True)
    shutil.copy2(backup,BACKEND)
    stop('Restored original after error: '+str(exc))
print('SUCCESS: Added LOCAL-ONLY /scenario_builder.html route to scenario_backend.py.')
print('Backend preparation behavior unchanged. Backup:',backup)
print('Restart the local Python scenario backend to load this route.')
