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
import time
from http import cookies
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

RANGES: dict[str, int] = {"1h": 3600, "24h": 86_400,
                          "7d": 604_800, "90d": 7_776_000}
# Each stream holds a thread. Past this, the client falls back to polling.
MAX_STREAMS = 8
# Ranges up to this length are served from the raw table; longer ones from
# the 5-minute aggregates.
RAW_TABLE_MAX_SECONDS = 172_800

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

# Status codes the base class can raise itself (an unsupported HTTP verb, a
# malformed request line) before our own routing ever runs. Mapped to a
# machine-readable code so those responses stay code-shaped like every other
# error body, instead of falling back to the base class's HTML page.
FALLBACK_ERROR_CODES = {
    400: "bad_request",
    501: "not_implemented",
}


SESSION_COOKIE_NAME = "health_token"


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


def cookie_token(header_value: str | None) -> str | None:
    """The health_token cookie value, or None for anything else.

    A malformed Cookie header must never raise: it would take the whole
    request down over a header this process does not control.
    """
    if not header_value:
        return None
    jar = cookies.SimpleCookie()
    try:
        jar.load(header_value)
    except cookies.CookieError:
        return None
    morsel = jar.get(SESSION_COOKIE_NAME)
    return morsel.value if morsel else None


def session_cookie_header(token: str) -> str:
    # No Secure flag: this is plain HTTP on a LAN, and Secure would make the
    # browser never send the cookie back over it.
    return f"{SESSION_COOKIE_NAME}={token}; HttpOnly; SameSite=Strict; Path=/"


def make_server(cfg: Config, scheduler,
                web_dir: Path = WEB_DIR) -> ThreadingHTTPServer:

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        _stream_lock = threading.Lock()
        _stream_count = 0

        def log_message(self, *args):     # quiet: logged elsewhere
            pass

        # --- helpers ----------------------------------------------
        def _send(self, code: int, body: bytes, content_type: str,
                  extra_headers: dict[str, str] | None = None):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in SECURITY_HEADERS.items():
                self.send_header(name, value)
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            # RFC 9110 SS9.3.2: a HEAD response carries the header fields
            # the equivalent GET would have returned -- Content-Length
            # included -- but never a body, so only the write is skipped.
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, payload: dict,
                  extra_headers: dict[str, str] | None = None):
            self._send(code, json.dumps(payload).encode("utf-8"),
                       "application/json; charset=utf-8", extra_headers)

        def _error(self, code: int, error: str, detail: str = "",
                   extra_headers: dict[str, str] | None = None):
            # Machine-readable codes, never user-facing prose: the browser
            # localises from its catalogue.
            self._json(code, {"error": error, "detail": detail}, extra_headers)

        def send_error(self, code, message=None, explain=None):
            # The base class calls this directly for errors it detects
            # itself (an unsupported HTTP verb, a malformed request line)
            # before our own do_GET ever runs. Without this override those
            # responses would carry the base class's HTML body and none of
            # our security headers -- "security headers on every response"
            # would not hold.
            #
            # Connection: close is sent only here, not from _send: the
            # base class's own send_error always sends it, and
            # send_header('Connection', 'close') has the side effect of
            # forcing close_connection = True -- a safety net for the
            # narrow cases where parse_request() left close_connection
            # False before an error fired (an overlong HTTP/1.1 request
            # line, or an HTTP/2.0 request line). A normal 200 response
            # must not carry this, so it stays out of _send.
            self._error(code, FALLBACK_ERROR_CODES.get(code, "http_error"),
                       "", {"Connection": "close"})

        def _authorised(self, query) -> tuple[bool, str | None]:
            # In order of preference: the X-Health-Token header; ?k=<token>,
            # which exists only so a phone opening a shared or bookmarked
            # link can authenticate -- plain navigation has no way to set a
            # header; then the health_token session cookie set by an
            # earlier ?k= handoff (see do_GET). log_message() above keeps
            # the token out of this process's own access log, and
            # Referrer-Policy: no-referrer keeps it out of cross-navigation
            # Referer headers, but neither reaches browser history,
            # bookmarks, or an intermediary's own logs (LAN router, proxy,
            # connection tracking) -- which is why a successful ?k= request
            # immediately hands off to a cookie instead of relying on the
            # query string for every subsequent request.
            header_token = self.headers.get("X-Health-Token")
            query_token = query.get("k", [None])[0]
            presented = header_token
            if presented is None:
                presented = query_token
            if presented is None:
                presented = cookie_token(self.headers.get("Cookie"))
            client_ip = self.client_address[0]
            ok = authorise(client_ip, presented, cfg)
            # Only hand off when the query parameter is actually what
            # authorised this request -- not on loopback (which needs no
            # token at all) and not when a header already did the job.
            handoff = (query_token
                       if ok and header_token is None
                       and query_token is not None
                       and not is_loopback(client_ip)
                       else None)
            return ok, handoff

        def _serve_file(self, relative: str,
                        extra_headers: dict[str, str] | None = None):
            target = (web_dir / relative).resolve()
            try:
                target.relative_to(web_dir.resolve())
            except (ValueError, OSError):
                return self._error(403, "path_refused", relative)
            if not target.is_file():
                return self._error(404, "file_not_found", relative)
            self._send(200, target.read_bytes(),
                       CONTENT_TYPES.get(target.suffix,
                                         "application/octet-stream"),
                       extra_headers)

        def _history(self, query, extra_headers: dict[str, str] | None = None):
            metric = query.get("metric", [None])[0]
            window = query.get("range", ["24h"])[0]
            if not metric:
                return self._error(400, "missing_parameter", "metric")
            if window not in RANGES:
                return self._error(400, "unknown_range",
                                   ", ".join(RANGES))
            now = int(time.time())
            since = now - RANGES[window]
            table = ("metric" if RANGES[window] <= RAW_TABLE_MAX_SECONDS
                     else "metric_5m")
            points = scheduler.store.read_series(metric, since, now, table=table)
            depth = scheduler.store.available_depth_seconds(table, now)
            return self._json(200, {
                "metric": metric, "range": window, "table": table,
                "points": [[ts, value] for ts, value in points],
                # Always the depth actually available: a half-empty "90 days"
                # chart would suggest a collection failure when the history
                # has simply just begun.
                "depth_days": round(depth / 86_400, 2),
            }, extra_headers)

        def _stream(self, extra_headers: dict[str, str] | None = None):
            with Handler._stream_lock:
                if Handler._stream_count >= MAX_STREAMS:
                    return self._error(503, "too_many_streams", str(MAX_STREAMS))
                Handler._stream_count += 1
            try:
                self.send_response(200)
                self.send_header("Content-Type",
                                 "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                # Without Content-Length, HTTP/1.1 must close the connection at
                # the end of the stream: otherwise the client cannot tell where
                # the body ends and the next response is misframed.
                self.send_header("Connection", "close")
                self.close_connection = True
                for name, value in SECURITY_HEADERS.items():
                    self.send_header(name, value)
                # EventSource cannot set a custom header, so a stream opened
                # straight from a ?k= link relies on this cookie handoff --
                # it is the primary mechanism, not a belt-and-braces extra.
                for name, value in (extra_headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                while True:
                    payload = json.dumps(scheduler.state())
                    self.wfile.write(b"event: state\n")
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    time.sleep(cfg.sampling.live_seconds)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with Handler._stream_lock:
                    Handler._stream_count -= 1

        # --- routing ----------------------------------------------
        def do_GET(self):
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)

            authorised, handoff = self._authorised(query)
            if not authorised:
                return self._error(401, "token_required", "")
            # A request that only authorised because of ?k= hands the token
            # off to a cookie on its own response, so every subsequent
            # request from the same browser -- static subresources, /api/now,
            # /api/stream -- is authorised without the query string, which
            # plain <link>/<script>/fetch/EventSource requests never carry.
            extra = ({"Set-Cookie": session_cookie_header(handoff)}
                     if handoff else None)

            if parsed.path == "/":
                return self._serve_file("index.html", extra)
            if parsed.path.startswith("/static/"):
                return self._serve_file(parsed.path[len("/static/"):], extra)
            if parsed.path == "/api/now":
                return self._json(200, scheduler.state(), extra)
            if parsed.path == "/api/history":
                return self._history(query, extra)
            if parsed.path == "/api/stream":
                return self._stream(extra)
            return self._error(404, "unknown_route", parsed.path)

    server = ThreadingHTTPServer((cfg.bind, cfg.port), Handler)
    server.daemon_threads = True
    return server
