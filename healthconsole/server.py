"""HTTP server.

The console listens on the local network: any non-loopback access requires the
token, compared in constant time. Reading is open; acting is not — but actions
belong to plan 3.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from healthconsole.config import Config

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def is_loopback(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def authorise(client_ip: str, presented: str | None, cfg: Config) -> bool:
    if is_loopback(client_ip):
        return True
    if not cfg.token or not presented:
        return False
    return hmac.compare_digest(cfg.token, presented)


def make_server(cfg: Config, scheduler,
                web_dir: Path = WEB_DIR) -> ThreadingHTTPServer:

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        _stream_lock = threading.Lock()
        _stream_count = 0

        def log_message(self, *args):     # quiet: logged elsewhere
            pass

        # --- helpers ----------------------------------------------
        def _send(self, code: int, body: bytes, content_type: str):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in SECURITY_HEADERS.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload: dict):
            self._send(code, json.dumps(payload).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _error(self, code: int, error: str, detail: str = ""):
            # Machine-readable codes, never user-facing prose: the browser
            # localises from its catalogue.
            self._json(code, {"error": error, "detail": detail})

        def _authorised(self, query) -> bool:
            presented = (self.headers.get("X-Health-Token")
                         or query.get("k", [None])[0])
            return authorise(self.client_address[0], presented, cfg)

        def _serve_file(self, relative: str):
            target = (web_dir / relative).resolve()
            try:
                target.relative_to(web_dir.resolve())
            except ValueError:
                return self._error(403, "path_refused", relative)
            if not target.is_file():
                return self._error(404, "file_not_found", relative)
            self._send(200, target.read_bytes(),
                       CONTENT_TYPES.get(target.suffix,
                                         "application/octet-stream"))

        # --- routing ----------------------------------------------
        def do_GET(self):
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)

            if not self._authorised(query):
                return self._error(401, "token_required",
                                   "non-loopback access requires a token")

            if parsed.path == "/":
                return self._serve_file("index.html")
            if parsed.path.startswith("/static/"):
                return self._serve_file(parsed.path[len("/static/"):])
            if parsed.path == "/api/now":
                return self._json(200, scheduler.state())
            return self._error(404, "unknown_route", parsed.path)

    server = ThreadingHTTPServer((cfg.bind, cfg.port), Handler)
    server.daemon_threads = True
    return server
