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
import sqlite3
import sys
import threading
import time
from collections import deque
from collections.abc import Mapping
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import MappingProxyType
from urllib.parse import parse_qs, urlparse

from healthconsole.actions import CATALOGUE, Action
from healthconsole.config import Config
from healthconsole.runner import ActionBusy, ActionRunner

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

# img-src widened to allow `data:` alongside 'self': Bootstrap's vendored
# stylesheet (web/vendor/bootstrap.min.css) embeds its icons as inline
# data:image/svg+xml URIs, and two of them are on this page -- the accordion
# chevron (.accordion-button::after) and the select arrow (.form-select).
# Under plain `default-src 'self'` the browser blocks both as an img-src
# violation and drops them silently: the accordion and language selector
# still work, they just render with no icon. This widening is images only —
# script-src and style-src are untouched, and an SVG referenced as an image
# renders in a restricted mode where scripts, external references and
# interaction are all inert, so a data: URI here cannot execute anything.
# Do not narrow this back to `default-src 'self'` alone.
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:",
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

# The console's own UI sets this header on every action POST. Its whole
# job is to be unforgeable from another origin: a cross-origin HTML form
# cannot set a request header at all, and a cross-origin fetch() that
# sets one turns the request into a preflighted request -- which this
# server answers with no CORS headers and no OPTIONS handler, so the
# browser never sends the POST. See do_POST.
ACTION_INTENT_HEADER = "X-Health-Action"
ACTION_INTENT_VALUE = "run"

# The relay below is a live view for whichever tab is open, not a
# transcript: the audit row keeps every line regardless.
ACTION_EVENT_QUEUE_MAX = 200

# No route here reads a request body, so any body is refused -- but it is
# drained first, up to this size, so a well-behaved client that sent one
# keeps a usable connection. Past this, the connection is dropped instead
# of reading megabytes into memory only to discard them.
MAX_DRAINABLE_BODY = 65_536


def is_loopback(addr: str) -> bool:
    try:
        address = ipaddress.ip_address(addr)
    except ValueError:
        return False
    # With bind = "::", an IPv4 client connecting to the machine itself
    # arrives IPv4-mapped (::ffff:127.0.0.1), which IPv6Address.is_loopback
    # does not recognise as loopback on its own. Unwrap it first, or that
    # client is refused as if it were remote -- with no `token` command to
    # get in some other way until this same review wave added one.
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return address.is_loopback


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


def make_server(cfg: Config, scheduler, web_dir: Path = WEB_DIR, *,
                runner: ActionRunner | None = None,
                catalogue: Mapping[str, Action] = MappingProxyType(CATALOGUE),
                ) -> ThreadingHTTPServer:

    # cmd_run's shutdown sequence closes the listening socket
    # (server.server_close()) and only then closes the store -- but
    # server_close() does not wait for request threads already in flight,
    # and daemon_threads=True means nothing else will either (the /api/stream
    # handler loops forever by design, so joining it would hang shutdown).
    # Set by cmd_run just before it closes the store, this flag lets a
    # request that starts in that window (an existing keep-alive connection
    # can still send one after the listening socket is gone) fail fast with
    # a 503 instead of reaching the store at all. It cannot wedge a normal
    # request: checking it never blocks, and it is only ever set once, at
    # shutdown. A request already past this check when the store closes is
    # covered separately -- see the try/except around the store calls in
    # _history below.
    shutdown_event = threading.Event()

    # A bounded, shared feed of action events for /api/stream to relay as
    # `event: action`. Kept small on purpose: a run's full output is
    # already written to the audit row by ActionRunner, so this is a live
    # view for whichever tab happens to be open, not a transcript. Each
    # entry is (seq, event); seq is a simple increasing counter so a
    # stream can remember how far it has read without destructively
    # draining a queue that other, concurrent streams also need to see.
    #
    # ActionRunner's own docs (Task 3) are explicit that `on_event` can be
    # called from more than one run's delivery thread at once -- is_busy()
    # going false does not mean the previous run's "finished" has been
    # delivered yet, so a new run's events and the tail of the old run's
    # events can arrive here concurrently. Nothing above a plain lock is
    # needed since this function only ever appends, but the lock is what
    # makes that append/increment atomic across those threads.
    action_events_lock = threading.Lock()
    # Deliberately unbounded at the deque level: `deque(maxlen=)` evicts
    # from the left whatever happens to be there, which silently loses the
    # two events a client cannot reconstruct -- a run that never appears
    # to start, or never appears to end. broadcast_action below enforces
    # the bound itself, dropping only "output".
    action_events: deque[tuple[int, dict]] = deque()
    action_seq = 0
    # A one-element list rather than a nonlocal int so the stream loop and
    # the tests can read the running total by reference.
    action_relay_dropped = [0]

    def broadcast_action(event: dict) -> None:
        nonlocal action_seq
        first_drop = False
        with action_events_lock:
            if len(action_events) >= ACTION_EVENT_QUEUE_MAX:
                # Mirrors ActionRunner._drop_one_locked, and for the same
                # reason: the live stream is worth less than an unbounded
                # backlog, but "started" and "finished" are the two events
                # a client cannot infer from anything else, and the audit
                # row keeps every output line regardless. Scanning is an
                # O(1) popleft in practice -- the leftmost entry is almost
                # always an "output" one.
                for index, (_, queued) in enumerate(action_events):
                    if queued.get("phase") == "output":
                        del action_events[index]
                        break
                else:
                    action_events.popleft()
                action_relay_dropped[0] += 1
                first_drop = action_relay_dropped[0] == 1
            action_seq += 1
            action_events.append((action_seq, event))
        # Logged outside the lock, exactly as ActionRunner does it: this
        # runs on a run's delivery thread, and writing to stderr is an
        # unbounded wait once a pipe nobody reads fills up.
        if first_drop:
            print("the action event relay is full; dropping the oldest "
                  "output events from the live stream (the audit row "
                  "still records all of them)", file=sys.stderr)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        # Without this, a client that walks off the LAN (a phone losing
        # wifi mid-request) holds its thread -- and, for /api/stream, one
        # of the MAX_STREAMS slots -- until TCP notices on its own, which
        # for a half-open connection can be effectively never.
        timeout = 30
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
            if shutdown_event.is_set():
                return self._error(503, "shutting_down", "")
            now = int(time.time())
            since = now - RANGES[window]
            table = ("metric" if RANGES[window] <= RAW_TABLE_MAX_SECONDS
                     else "metric_5m")
            try:
                points = scheduler.store.read_series(metric, since, now,
                                                     table=table)
                depth = scheduler.store.available_depth_seconds(
                    table, now, metric=metric)
            except sqlite3.Error as exc:
                # Primarily the narrow shutdown race described above -- a
                # request thread already inside the store when cmd_run's
                # finally block closes it out from under this one -- but
                # any other sqlite3 failure (a locked or corrupt database)
                # deserves the same treatment: a diagnostic tool must not
                # crash a request thread over its own storage acting up.
                # web/js/history.js turns any non-OK response into a
                # HistoryError, which the front end renders as
                # ui.error.history -- nothing more is needed client-side.
                return self._error(503, "store_unavailable",
                                   type(exc).__name__)
            return self._json(200, {
                "metric": metric, "range": window, "table": table,
                "points": [[ts, value] for ts, value in points],
                # Always the depth actually available: a half-empty "90 days"
                # chart would suggest a collection failure when the history
                # has simply just begun.
                "depth_days": round(depth / 86_400, 2),
            }, extra_headers)

        def _action_runs(self, extra_headers: dict[str, str] | None = None):
            if shutdown_event.is_set():
                return self._error(503, "shutting_down", "")
            try:
                runs = scheduler.store.read_action_runs()
            except sqlite3.Error as exc:
                # Same narrow shutdown race as _history above: a request
                # thread already inside the store when cmd_run's finally
                # block closes it out from under this one.
                return self._error(503, "store_unavailable",
                                   type(exc).__name__)
            return self._json(200, {"runs": runs}, extra_headers)

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
                # Only events broadcast from here on: a tab that opens
                # mid-run sees the run's progress going forward, not a
                # replay of everything still sitting in the bounded
                # queue -- the audit row is where history lives.
                with action_events_lock:
                    last_action_seq = action_seq
                    last_dropped = action_relay_dropped[0]
                while True:
                    payload = json.dumps(scheduler.state())
                    self.wfile.write(b"event: state\n")
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    with action_events_lock:
                        pending = [item for item in action_events
                                  if item[0] > last_action_seq]
                        dropped_now = action_relay_dropped[0]
                    # A gap in the relay is reported rather than papered
                    # over: a client that sees output stop mid-run should
                    # be able to tell "the console dropped lines" from
                    # "the command went quiet".
                    if dropped_now > last_dropped:
                        gap = {"phase": "relay_gap",
                               "lost": dropped_now - last_dropped}
                        last_dropped = dropped_now
                        self.wfile.write(b"event: action\n")
                        self.wfile.write(
                            f"data: {json.dumps(gap)}\n\n".encode("utf-8"))
                    for seq, event in pending:
                        last_action_seq = seq
                        self.wfile.write(b"event: action\n")
                        self.wfile.write(
                            f"data: {json.dumps(event)}\n\n".encode("utf-8"))
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
            if parsed.path == "/api/actions":
                # Locale-neutral: ids and risk levels only, never prose --
                # the browser localises from its own catalogue, so one
                # response serves every language.
                return self._json(200, [
                    # `available` is always true in B1: every gate that
                    # could make it false reads probe data that does not
                    # exist yet. It ships anyway so the front end honours
                    # it from the start rather than being retrofitted.
                    {"id": action.id, "risk": action.risk.value,
                     "available": True}
                    for action in catalogue.values()], extra)
            if parsed.path == "/api/actions/runs":
                return self._action_runs(extra)
            return self._error(404, "unknown_route", parsed.path, extra)

        # Without this, BaseHTTPRequestHandler answers any HEAD request with
        # send_error(501) before do_GET ever runs -- routing, auth and the
        # 200 status a HEAD is supposed to mirror never happen at all. _send
        # already omits the body for self.command == "HEAD", so sharing
        # do_GET's routing is all that is needed.
        do_HEAD = do_GET

        def _consume_body(self) -> str | None:
            """Consume this request's body. Returns an error code, or None.

            No route here reads a body, so bytes a client announces and
            we never read stay in the socket buffer and are parsed as the
            *next* request line on a keep-alive connection. A cross-origin
            `<form enctype="text/plain">` controls those bytes exactly,
            which turns one forged POST into two chosen actions on one
            connection. Benignly, any client doing `fetch(url, {body})`
            desynchronises the same way.
            """
            if (self.headers.get("Transfer-Encoding") or "").strip():
                # Chunked framing: the body's length is not knowable from
                # the headers, so the only safe move is to stop reusing
                # this connection rather than guess where it ends.
                self.close_connection = True
                return "body_refused"
            raw = self.headers.get("Content-Length")
            if raw is None:
                return None
            try:
                length = int(raw)
            except ValueError:
                self.close_connection = True
                return "bad_content_length"
            if length < 0:
                self.close_connection = True
                return "bad_content_length"
            if length == 0:
                return None
            if length > MAX_DRAINABLE_BODY:
                self.close_connection = True
                return "body_refused"
            try:
                self.rfile.read(length)
            except OSError:
                # A client that announced more than it sent: the read hit
                # this handler's socket timeout. Nothing left to trust
                # about the framing.
                self.close_connection = True
            return "body_refused"

        def do_POST(self):
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)

            # First, before any routing or refusal: whatever this request
            # claims its body is must not survive into the next request
            # on this connection, on every path out of this method.
            body_error = self._consume_body()

            authorised, handoff = self._authorised(query)
            if not authorised:
                return self._error(401, "token_required", "")
            # Every refusal below carries the handoff too, not just the
            # 202: a LAN client that authenticated with ?k= must get its
            # session cookie even when the answer is no, or its next
            # request falls back to the query string this hands off to
            # stop using.
            extra = ({"Set-Cookie": session_cookie_header(handoff)}
                     if handoff else None)

            if body_error is not None:
                return self._error(400, body_error, "", extra)

            prefix = "/api/actions/"
            if not parsed.path.startswith(prefix):
                return self._error(404, "unknown_route", parsed.path, extra)

            # --- CSRF -------------------------------------------------
            #
            # This is the console's only side-effecting route, and
            # authorise() short-circuits to True for loopback before a
            # token is ever consulted -- so this route needs no credential
            # of any kind. That was harmless while every route was a read
            # whose response the browser's same-origin policy already hid;
            # it is not harmless now. Any page the operator has open can
            # auto-submit a form at http://127.0.0.1:8787/api/actions/...
            # and run a root command. The attacker never sees the
            # response and does not need to: the side effect is the
            # payload. SameSite=Strict on the session cookie defends
            # nothing here, because loopback sends no cookie at all.
            #
            # Gate 1 -- a header no cross-origin request can arrive
            # carrying. An HTML form cannot set a request header at all,
            # and a fetch() that sets one stops being a "simple request",
            # so the browser preflights it with OPTIONS; this server
            # implements no OPTIONS handler and returns no CORS headers,
            # so the browser never sends the POST. Private Network Access
            # preflights are deliberately not relied on: partial, and
            # browser-dependent.
            if self.headers.get(ACTION_INTENT_HEADER) != ACTION_INTENT_VALUE:
                return self._error(403, "action_intent_required", "", extra)
            # Gate 2 -- belt and braces, one line, for a browser that
            # somehow reaches here with the header set. Only fires when
            # the browser labelled the request itself: absence means a
            # non-browser client (curl, a script, this suite), which gate
            # 1 already covers, and treating absence as hostile would lock
            # every one of them out.
            fetch_site = self.headers.get("Sec-Fetch-Site")
            if fetch_site is not None and fetch_site != "same-origin":
                return self._error(403, "cross_site_refused", "", extra)

            # Reading is open to the LAN behind a token; acting is not. A
            # token proves who you are, not that you are sitting at this
            # machine, and the two do not carry the same cost when wrong.
            if not is_loopback(self.client_address[0]) \
                    and not cfg.allow_remote_actions:
                return self._error(403, "remote_actions_refused", "", extra)

            # Resolved against the catalogue this server was built with --
            # never the module-level lookup() -- so a test can swap in a
            # harmless catalogue instead of shelling out to apt-get.
            action = catalogue.get(parsed.path[len(prefix):])
            if action is None:
                return self._error(404, "unknown_action", "", extra)

            # Guarded here as well as in _history and _action_runs, and it
            # matters more here than in either: server_close() does not
            # wait for request threads, so a keep-alive connection a
            # browser opened earlier can still deliver a POST after
            # cmd_run has shut the runner down and closed the store.
            # Starting a run in that window leaves a child nothing will
            # ever reap -- with the real catalogue, apt-get running as
            # root past the console's own exit -- and its audit write
            # lands on a closed database.
            if shutdown_event.is_set():
                return self._error(503, "shutting_down", "", extra)

            if runner is None:
                # Should not happen in production -- cli.py always wires
                # one in -- but a server built without one (as most tests
                # here are) must refuse cleanly rather than raise.
                return self._error(503, "runner_unavailable", "", extra)

            try:
                run_id = runner.start(action, self.client_address[0],
                                      broadcast_action)
            except ActionBusy:
                return self._error(409, "action_busy", "", extra)
            except Exception as exc:                  # noqa: BLE001
                # Anything else would escape the handler with no response
                # written at all: the client sees the connection drop and
                # this process prints a traceback. A diagnostic tool
                # answers even when its own machinery is what failed.
                return self._error(500, "action_failed",
                                   type(exc).__name__, extra)
            return self._json(202, {"run_id": run_id}, extra)

    server = ThreadingHTTPServer((cfg.bind, cfg.port), Handler)
    server.daemon_threads = True
    # cmd_run sets this just before closing the store -- see the comment
    # above shutdown_event's definition.
    server.shutdown_event = shutdown_event
    # The action relay, exposed the same way shutdown_event is: cmd_run
    # needs the flag, and the relay's drop policy is worth testing
    # directly rather than only through a timing-dependent live stream.
    server.broadcast_action = broadcast_action
    server.action_events = action_events
    server.action_relay_dropped = action_relay_dropped
    return server
