#!/usr/bin/env python3
"""Read-only, training-only file server for WDS preparation; no upload/write routes.

Do not offer directory listing or serve Python, cache, config, Git, or unprepared data.
The only JSON visible to the browser is within a fully prepared case folder.
"""
from __future__ import annotations
from http.server import SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import unquote, urlsplit
import re
ROOT = Path(__file__).resolve().parent
CASE_RE = re.compile(r"^/web_data/scenarios/[0-9a-f]{16}/(?:manifest\.json|basemap_static\.json|warning_geography\.json|storm_reports\.json|(?:[A-Za-z0-9_-]+/){1,3}[A-Za-z0-9_.-]{1,90}\.gz)$")
PAGE_NAMES = {'/index.html', '/geo_viewer.html', '/scenario_builder.html', '/review_results.html', '/favicon.ico'}

class SafeHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def _allowed(self):
        raw = urlsplit(self.path).path
        if '%' in raw:
            # Reject encoded slashes/dot segments and inconsistent normalization.
            return None
        if raw == '/': raw = '/index.html'
        if raw not in PAGE_NAMES and not CASE_RE.fullmatch(raw): return None
        rel = raw.lstrip('/')
        file = (ROOT / rel).resolve()
        if not file.is_relative_to(ROOT.resolve()) or not file.is_file():return None
        if file.is_symlink() or any((ROOT / p).is_symlink() for p in Path(rel).parents if p != Path('.')):return None
        return file

    def do_GET(self):
        path = self._allowed()
        if path is None: return self.send_error(404, 'Not found')
        self._serve(path, head=False)

    def do_HEAD(self):
        path = self._allowed()
        if path is None: return self.send_error(404, 'Not found')
        self._serve(path, head=True)

    def _serve(self, file, head):
        import mimetypes
        data_size = file.stat().st_size
        content_type = mimetypes.guess_type(str(file))[0] or 'application/octet-stream'
        if file.suffix == '.gz': content_type = 'application/gzip'
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(data_size))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "frame-ancestors 'none'")
        self.end_headers()
        if not head:
            with file.open('rb') as f:
                import shutil
                shutil.copyfileobj(f, self.wfile)
