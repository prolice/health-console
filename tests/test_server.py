import inspect
import json
import socket
import sqlite3
import threading
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from unittest import mock

from healthconsole.actions import Action, Risk
from healthconsole.cli import cmd_run
from healthconsole.config import Config
from healthconsole.ring import Ring
from healthconsole.runner import ActionRunner
from healthconsole.scheduler import Scheduler
from healthconsole.server import (
    ACTION_EVENT_QUEUE_MAX, ACTION_INTENT_HEADER, ACTION_INTENT_VALUE,
    authorise, cookie_token, generate_token, is_loopback, make_server,
    session_cookie_header,
)
from healthconsole.store import Store

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class TestLoopback(unittest.TestCase):
    def test_ipv4_loopback(self):
        self.assertTrue(is_loopback("127.0.0.1"))

    def test_ipv6_loopback(self):
        self.assertTrue(is_loopback("::1"))

    def test_lan_address_is_not_loopback(self):
        self.assertFalse(is_loopback("192.168.0.3"))

    def test_garbage_is_not_loopback(self):
        self.assertFalse(is_loopback("not-an-address"))

    def test_ipv4_mapped_loopback_is_loopback(self):
        # With bind = "::", an IPv4 client connecting to the machine itself
        # arrives as ::ffff:127.0.0.1. Without unwrapping it, that client
        # would be refused as if it came from off-machine -- and, before
        # this review wave added a `token` command, would have had no way
        # in at all.
        self.assertTrue(is_loopback("::ffff:127.0.0.1"))

    def test_ipv4_mapped_lan_address_is_not_loopback(self):
        self.assertFalse(is_loopback("::ffff:192.168.0.3"))


class TestAuthorise(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(token="s3cr3t")

    def test_loopback_needs_no_token(self):
        self.assertTrue(authorise("127.0.0.1", None, self.cfg))

    def test_lan_without_token_is_refused(self):
        self.assertFalse(authorise("192.168.0.3", None, self.cfg))

    def test_lan_with_wrong_token_is_refused(self):
        self.assertFalse(authorise("192.168.0.3", "wrong", self.cfg))

    def test_lan_with_right_token_is_allowed(self):
        self.assertTrue(authorise("192.168.0.3", "s3cr3t", self.cfg))

    def test_empty_configured_token_never_authorises_the_lan(self):
        # An empty token must not open access to the whole network.
        self.assertFalse(authorise("192.168.0.3", "", Config(token="")))


class TestToken(unittest.TestCase):
    def test_token_is_long_and_random(self):
        first, second = generate_token(), generate_token()
        self.assertNotEqual(first, second)
        self.assertGreaterEqual(len(first), 43)


class TestHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = Store(":memory:")
        cls.scheduler = Scheduler(Config(), cls.store, Ring())
        cls.scheduler.tick(now=1000.0)
        cls.server = make_server(Config(bind="127.0.0.1", port=0),
                                 cls.scheduler, WEB_DIR)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.store.close()

    def get(self, path):
        return urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}{path}", timeout=5)

    def test_api_now_returns_the_state(self):
        payload = json.loads(self.get("/api/now").read())
        self.assertIn("score", payload)
        self.assertIn("probes", payload)
        self.assertIn("depth_days", payload)

    def test_index_is_served(self):
        response = self.get("/")
        self.assertEqual(response.status, 200)
        self.assertIn("text/html", response.headers["Content-Type"])

    def test_unknown_route_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/nonexistent")
        self.assertEqual(ctx.exception.code, 404)

    def test_security_headers_are_present(self):
        headers = self.get("/").headers
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        # Without this clause, Bootstrap's inline data:image/svg+xml icons
        # (the accordion chevron and the select arrow) are silently dropped
        # by the browser as an img-src violation -- this assertion alone,
        # unlike the default-src one above, catches that clause being
        # dropped again.
        self.assertIn("img-src 'self' data:",
                      headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_path_traversal_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/static/../../etc/passwd")
        self.assertIn(ctx.exception.code, (403, 404))

    def test_error_body_is_machine_readable(self):
        # Errors carry codes, not user-facing prose: the browser localises.
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/nonexistent")
        payload = json.loads(ctx.exception.read())
        self.assertIn("error", payload)
        self.assertIn("detail", payload)
        self.assertNotIn(" ", payload["error"])
        self.assertEqual(payload["error"], payload["error"].lower())
        if payload["detail"]:
            self.assertNotIn(" ", payload["detail"])

    def test_unsupported_method_still_gets_security_headers_and_json(self):
        # The base class handles unknown verbs itself, before our routing
        # ever runs -- that path must not bypass the security headers or
        # fall back to an HTML body. PUT, not POST: POST is now a real verb
        # (it starts a catalogue action), so it no longer exercises this
        # path -- see TestActionPost for POST's own routing.
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/now", method="PUT")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(ctx.exception.code, 501)
        self.assertIn("default-src 'self'",
                      ctx.exception.headers["Content-Security-Policy"])
        payload = json.loads(ctx.exception.read())
        self.assertIn("error", payload)

    def test_error_response_carries_connection_close(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/now", method="PUT")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(ctx.exception.headers["Connection"], "close")

    def test_handler_class_declares_a_connection_timeout(self):
        # Without this, a client that walks off the LAN mid-request holds
        # its thread -- and, for /api/stream, one of the MAX_STREAMS slots
        # -- until TCP notices on its own, which may be never.
        self.assertEqual(self.server.RequestHandlerClass.timeout, 30)

    def test_head_request_sends_headers_but_no_body(self):
        # http.client and urllib both hide a missing HEAD-body guard --
        # their HEAD-aware readers stop at the headers regardless of what
        # is actually on the wire. A raw socket is the only way to see it.
        with socket.create_connection(
                ("127.0.0.1", self.port), timeout=5) as sock:
            sock.settimeout(2)
            sock.sendall(b"HEAD /api/now HTTP/1.1\r\nHost: localhost\r\n\r\n")
            chunks = []
            try:
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
            except socket.timeout:
                pass
        raw = b"".join(chunks)
        status_line, _, _ = raw.partition(b"\r\n")
        headers, _, body = raw.partition(b"\r\n\r\n")
        self.assertIn(b"200", status_line)
        self.assertIn(b"Content-Length", headers)
        self.assertEqual(body, b"")


class TestHistoryEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = Store(":memory:")
        cls.scheduler = Scheduler(Config(), cls.store, Ring())
        cls.scheduler.tick(now=1000.0)
        cls.server = make_server(Config(bind="127.0.0.1", port=0),
                                 cls.scheduler, WEB_DIR)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.store.close()

    def tearDown(self):
        self.server.shutdown_event.clear()

    def get(self, path):
        return urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}{path}", timeout=5)

    def test_history_returns_points_and_depth(self):
        payload = json.loads(
            self.get("/api/history?metric=cpu.usage&range=24h").read())
        self.assertIn("points", payload)
        self.assertIn("depth_days", payload)

    def test_store_error_during_history_is_a_503_not_a_crash(self):
        # Regression test for the shutdown race described in
        # healthconsole/server.py: a request thread already inside
        # store.read_series when cmd_run's finally block closes the store
        # used to let sqlite3.InterfaceError escape do_GET and print a
        # traceback to the terminal. _history must turn any sqlite3.Error
        # into a machine-readable 503 instead of letting it propagate.
        with mock.patch.object(
                self.store, "read_series",
                side_effect=sqlite3.InterfaceError("bad parameter or other API misuse")):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.get("/api/history?metric=cpu.usage&range=24h")
            self.assertEqual(ctx.exception.code, 503)
            payload = json.loads(ctx.exception.read())
            self.assertEqual(payload["error"], "store_unavailable")
            self.assertNotIn(" ", payload["detail"])

    def test_shutdown_flag_short_circuits_before_touching_the_store(self):
        # Set by cmd_run just before it closes the store -- a request that
        # starts after that point must never reach the store at all.
        self.server.shutdown_event.set()
        with mock.patch.object(
                self.store, "read_series",
                side_effect=AssertionError("the store must not be touched")):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.get("/api/history?metric=cpu.usage&range=24h")
            self.assertEqual(ctx.exception.code, 503)
            payload = json.loads(ctx.exception.read())
            self.assertEqual(payload["error"], "shutting_down")


class TestCookieTokenParsing(unittest.TestCase):
    def test_absent_header_yields_no_token(self):
        self.assertIsNone(cookie_token(None))
        self.assertIsNone(cookie_token(""))

    def test_valid_cookie_is_read(self):
        self.assertEqual(cookie_token("health_token=abc123"), "abc123")

    def test_unrelated_cookie_yields_no_token(self):
        self.assertIsNone(cookie_token("other=value"))

    def test_malformed_header_does_not_raise(self):
        # http.cookies.SimpleCookie.load raises CookieError on a header
        # like this one; a bad Cookie header sent by anyone on the LAN must
        # not be able to take a request down with it.
        self.assertIsNone(cookie_token("====="))


class TestSessionCookieHeader(unittest.TestCase):
    def test_cookie_is_httponly_and_samesite_strict_without_secure(self):
        # No Secure flag: this is plain HTTP on a LAN, and Secure would stop
        # the browser sending the cookie back over it at all.
        header = session_cookie_header("abc123")
        self.assertIn("health_token=abc123", header)
        self.assertIn("HttpOnly", header)
        self.assertIn("SameSite=Strict", header)
        self.assertNotIn("Secure", header)


class ServerCase(unittest.TestCase):
    """Starts a real server against a real Store and Scheduler."""

    @classmethod
    def setUpClass(cls):
        cls.store = Store(":memory:")
        cls.scheduler = Scheduler(Config(), cls.store, Ring())
        cls.scheduler.tick(now=1000.0)
        cls.server = make_server(Config(bind="127.0.0.1", port=0),
                                 cls.scheduler, WEB_DIR)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.store.close()

    def get(self, path):
        return urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}{path}", timeout=5)

    def get_raw(self, path) -> str:
        return self.get(path).read().decode("utf-8")

    def get_json(self, path):
        return json.loads(self.get_raw(path))

    def get_status(self, path) -> int:
        try:
            return self.get(path).status
        except urllib.error.HTTPError as exc:
            return exc.code


class TestActionReadRoutes(ServerCase):
    def test_the_catalogue_lists_every_action_with_its_risk(self):
        body = self.get_json("/api/actions")
        by_id = {entry["id"]: entry for entry in body}
        self.assertEqual(set(by_id), {
            "apt.refresh", "apt.upgrade", "apt.security", "clean.aptcache",
        })
        self.assertEqual(by_id["apt.refresh"]["risk"], "safe")
        self.assertTrue(by_id["apt.refresh"]["available"])

    def test_export_returns_a_standalone_html_report(self):
        raw = self.get_raw("/api/export")
        self.assertIn("<!doctype html>", raw)
        self.assertIn("Health Console report", raw)

    def test_the_catalogue_carries_no_prose(self):
        # The API is locale-neutral: ids and risk levels only, so one
        # response serves both languages and switching needs no round trip.
        raw = self.get_raw("/api/actions")
        for word in ("Refresh", "Actualiser", "Safe", "Sans risque"):
            self.assertNotIn(word, raw)

    def test_the_audit_log_is_empty_before_anything_runs(self):
        self.assertEqual(self.get_json("/api/actions/runs")["runs"], [])

    def test_the_audit_log_returns_what_the_store_holds(self):
        self.store.write_action_run(
            "r1", 1000, "apt.refresh", "127.0.0.1", 0, 2140, "Done\n")
        runs = self.get_json("/api/actions/runs")["runs"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["action_id"], "apt.refresh")


class TestActionAuditLogEdgeCases(ServerCase):
    """Its own store, isolated from TestActionReadRoutes's ordering-sensitive
    'empty before anything runs' assertion.
    """

    def test_a_killed_run_reports_a_null_exit_code_not_zero(self):
        # exit_code is None for a run that was killed -- that distinction
        # is load-bearing and must survive into the JSON as null, not 0,
        # which would read as a success.
        self.store.write_action_run(
            "r2", 2000, "apt.refresh", "127.0.0.1", None, 30_000, "")
        runs = self.get_json("/api/actions/runs")["runs"]
        killed = next(run for run in runs if run["id"] == "r2")
        self.assertIsNone(killed["exit_code"])

    def test_store_error_during_the_audit_log_is_a_503_not_a_crash(self):
        with mock.patch.object(
                self.store, "read_action_runs",
                side_effect=sqlite3.InterfaceError(
                    "bad parameter or other API misuse")):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.get("/api/actions/runs")
            self.assertEqual(ctx.exception.code, 503)
            payload = json.loads(ctx.exception.read())
            self.assertEqual(payload["error"], "store_unavailable")

    def test_shutdown_flag_short_circuits_the_audit_log_too(self):
        self.server.shutdown_event.set()
        try:
            with mock.patch.object(
                    self.store, "read_action_runs",
                    side_effect=AssertionError(
                        "the store must not be touched")):
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    self.get("/api/actions/runs")
                self.assertEqual(ctx.exception.code, 503)
                payload = json.loads(ctx.exception.read())
                self.assertEqual(payload["error"], "shutting_down")
        finally:
            self.server.shutdown_event.clear()


class TestLanCookieHandoff(unittest.TestCase):
    """Exercises do_GET's cookie handoff for a non-loopback client.

    Real sockets in this test suite all connect over 127.0.0.1, so
    `is_loopback` is monkeypatched for the duration of each test to force
    the code down the LAN path -- the way the review brief suggested as an
    alternative to standing up a real non-loopback client.
    """

    @classmethod
    def setUpClass(cls):
        cls.store = Store(":memory:")
        cls.scheduler = Scheduler(Config(token="s3cr3t"), cls.store, Ring())
        cls.scheduler.tick(now=1000.0)
        cls.server = make_server(Config(bind="127.0.0.1", port=0, token="s3cr3t"),
                                 cls.scheduler, WEB_DIR)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.store.close()

    def setUp(self):
        patcher = mock.patch("healthconsole.server.is_loopback",
                             return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def test_query_token_returns_200_and_sets_a_session_cookie(self):
        response = urllib.request.urlopen(
            self.url("/api/now?k=s3cr3t"), timeout=5)
        self.assertEqual(response.status, 200)
        cookie = response.headers.get("Set-Cookie")
        self.assertIsNotNone(cookie)
        self.assertIn("health_token", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

    def test_header_token_does_not_trigger_a_cookie(self):
        request = urllib.request.Request(self.url("/api/now"))
        request.add_header("X-Health-Token", "s3cr3t")
        response = urllib.request.urlopen(request, timeout=5)
        self.assertEqual(response.status, 200)
        self.assertIsNone(response.headers.get("Set-Cookie"))

    def test_a_valid_cookie_alone_authorises_a_later_request(self):
        request = urllib.request.Request(self.url("/api/now"))
        request.add_header("Cookie", "health_token=s3cr3t")
        response = urllib.request.urlopen(request, timeout=5)
        self.assertEqual(response.status, 200)

    def test_wrong_cookie_is_refused(self):
        request = urllib.request.Request(self.url("/api/now"))
        request.add_header("Cookie", "health_token=wrong")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(ctx.exception.code, 401)

    def test_malformed_cookie_header_is_refused_not_crashed(self):
        request = urllib.request.Request(self.url("/api/now"))
        request.add_header("Cookie", "=====")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(ctx.exception.code, 401)

    def test_no_token_at_all_is_still_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.url("/api/now"), timeout=5)
        self.assertEqual(ctx.exception.code, 401)


class TestActionPost(ServerCase):
    """A harmless two-entry catalogue built on /bin/true and /bin/sleep --
    never apt-get -- with a real ActionRunner wired in exactly as cli.py
    wires one. `lookup()`/CATALOGUE are module-level globals with no
    injection seam, so this exercises make_server's own `catalogue` and
    `runner` keyword parameters instead of monkeypatching a global that
    would leak between tests.

    Builds its server lazily, on the first request a test makes, so a
    test can adjust `self.cfg` (allow_remote_actions, in particular)
    beforehand. Each test gets its own Store/Scheduler/ActionRunner/server
    from setUp -- unlike the class-shared server in ServerCase above --
    because busy-state (one test posts twice) must persist across calls
    within a test, but must not leak into the next one.
    """

    TEST_CATALOGUE = {
        "t.true": Action(id="t.true", argv=("/bin/true",), root=False,
                         risk=Risk.SAFE),
        "t.sleep": Action(id="t.sleep", argv=("/bin/sleep", "2"),
                          root=False, risk=Risk.SAFE),
    }

    def setUp(self):
        self.store = Store(":memory:")
        self.scheduler = Scheduler(Config(), self.store, Ring())
        self.scheduler.tick(now=1000.0)
        self.runner = ActionRunner(self.store)
        # A token is configured even though every test here connects over
        # real loopback: the off-loopback tests simulate a LAN client by
        # patching is_loopback (see post()), and authorise() then needs a
        # real token/header pair to succeed before the action-specific
        # loopback gate is ever reached.
        self.cfg = Config(bind="127.0.0.1", port=0, token="s3cr3t")
        self.server = None

    def tearDown(self):
        # Ends any run still in flight (t.sleep) before the store closes
        # under it -- the same ordering cmd_run's shutdown path must
        # follow (see cli.py). cancel() inside shutdown() is a no-op, not
        # an error, when nothing is running.
        self.runner.shutdown(timeout=5)
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        self.store.close()

    def _ensure_server(self):
        if self.server is not None:
            return
        self.server = make_server(
            self.cfg, self.scheduler, WEB_DIR,
            runner=self.runner, catalogue=self.TEST_CATALOGUE)
        self.port = self.server.server_address[1]
        threading.Thread(
            target=self.server.serve_forever, daemon=True).start()

    def get(self, path):
        self._ensure_server()
        return super().get(path)

    def post(self, path, client_ip=None):
        self._ensure_server()
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method="POST")
        request.add_header("X-Health-Token", self.cfg.token)
        request.add_header(ACTION_INTENT_HEADER, ACTION_INTENT_VALUE)
        # Real sockets in this suite all connect over 127.0.0.1; a
        # non-loopback client is simulated the same way TestLanCookieHandoff
        # does it above, by patching is_loopback for the request rather than
        # standing up an actual remote socket.
        patcher = None
        if client_ip is not None:
            patcher = mock.patch("healthconsole.server.is_loopback",
                                 return_value=False)
            patcher.start()
        try:
            try:
                response = urllib.request.urlopen(request, timeout=5)
                return response.status, json.loads(response.read())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read())
        finally:
            if patcher is not None:
                patcher.stop()

    def test_an_unknown_action_is_refused(self):
        status, body = self.post("/api/actions/rm.everything")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "unknown_action")

    def test_a_known_action_is_accepted_and_returns_a_run_id(self):
        status, body = self.post("/api/actions/t.true")
        self.assertEqual(status, 202)
        self.assertTrue(body["run_id"])

    def test_a_second_action_while_one_runs_is_refused(self):
        self.post("/api/actions/t.sleep")
        status, body = self.post("/api/actions/t.true")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "action_busy")

    def test_an_action_from_off_loopback_is_refused_by_default(self):
        # Reading and acting do not carry the same cost when you get it
        # wrong: a token is enough to read, never enough to act.
        status, body = self.post("/api/actions/t.true",
                                 client_ip="192.168.0.3")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "remote_actions_refused")

    def test_off_loopback_is_allowed_when_configured(self):
        self.cfg = replace(self.cfg, allow_remote_actions=True)
        status, _ = self.post("/api/actions/t.true", client_ip="192.168.0.3")
        self.assertEqual(status, 202)

    def test_get_on_the_action_route_is_not_a_way_to_run_it(self):
        # A route that acts must not be reachable by a link, a prefetch or
        # a crawler.
        self.assertEqual(self.get_status("/api/actions/t.true"), 404)

    # --- byte-exact replay -------------------------------------------
    #
    # urllib cannot emit what a browser emits for a cross-origin form
    # (it normalises headers and refuses to pipeline), and the request
    # smuggling below is a property of the exact bytes on the wire, so
    # these go out over a raw socket.

    def raw(self, payload: bytes, settle: float = 1.0) -> bytes:
        """Send exact bytes on one connection; read until close or silence."""
        self._ensure_server()
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            sock.sendall(payload)
            sock.settimeout(settle)
            chunks = []
            while True:
                try:
                    piece = sock.recv(65536)
                except OSError:          # includes socket.timeout
                    break
                if not piece:
                    break
                chunks.append(piece)
            return b"".join(chunks)
        finally:
            sock.close()

    def _cross_origin_form(self, action_id: str, extra: str = "") -> bytes:
        # Byte-for-byte what a browser sends for an auto-submitted
        # cross-origin <form method=POST>: an Origin it does not control,
        # Sec-Fetch-Site: cross-site, a form content type, and no token --
        # loopback needs none.
        return (
            f"POST /api/actions/{action_id} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.port}\r\n"
            "Origin: http://evil.example\r\n"
            "Sec-Fetch-Site: cross-site\r\n"
            "Sec-Fetch-Mode: navigate\r\n"
            "Content-Type: application/x-www-form-urlencoded\r\n"
            f"{extra}"
            "Content-Length: 0\r\n"
            "Connection: close\r\n"
            "\r\n").encode("ascii")

    def _same_origin_post(self, action_id: str, extra: str = "") -> bytes:
        return (
            f"POST /api/actions/{action_id} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.port}\r\n"
            f"X-Health-Token: {self.cfg.token}\r\n"
            f"{ACTION_INTENT_HEADER}: {ACTION_INTENT_VALUE}\r\n"
            "Sec-Fetch-Site: same-origin\r\n"
            f"{extra}"
            "Content-Length: 0\r\n"
            "Connection: close\r\n"
            "\r\n").encode("ascii")

    # --- CSRF: any page the operator visits must not be able to act ---

    def test_a_cross_origin_form_post_cannot_run_an_action(self):
        # The console runs on loopback, so authorise() waves the request
        # through without a token and SameSite has nothing to bite on:
        # a cross-origin form is a bare POST with no credentials at all.
        # The only thing that distinguishes it from the console's own
        # request is a header a form cannot set.
        self._ensure_server()
        with mock.patch.object(self.runner, "start",
                               wraps=self.runner.start) as start:
            reply = self.raw(self._cross_origin_form("t.true"))
        self.assertTrue(reply.startswith(b"HTTP/1.1 403"), reply[:120])
        self.assertIn(b"action_intent_required", reply)
        start.assert_not_called()

    def test_a_post_without_the_intent_header_is_refused(self):
        # Same-origin-shaped in every other way: it is the missing header
        # alone that refuses it, because that is the one signal no
        # cross-origin form and no preflight-free fetch can forge.
        self._ensure_server()
        payload = (
            f"POST /api/actions/t.true HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.port}\r\n"
            f"X-Health-Token: {self.cfg.token}\r\n"
            "Content-Length: 0\r\nConnection: close\r\n\r\n").encode("ascii")
        with mock.patch.object(self.runner, "start",
                               wraps=self.runner.start) as start:
            reply = self.raw(payload)
        self.assertTrue(reply.startswith(b"HTTP/1.1 403"), reply[:120])
        start.assert_not_called()

    def test_a_cross_site_fetch_is_refused_even_carrying_the_header(self):
        # Belt and braces for the second gate on its own: if some client
        # ever reaches this route with the intent header set but the
        # browser still labelling the request cross-site, refuse it.
        self._ensure_server()
        with mock.patch.object(self.runner, "start",
                               wraps=self.runner.start) as start:
            reply = self.raw(self._cross_origin_form(
                "t.true",
                extra=f"{ACTION_INTENT_HEADER}: {ACTION_INTENT_VALUE}\r\n"))
        self.assertTrue(reply.startswith(b"HTTP/1.1 403"), reply[:120])
        self.assertIn(b"cross_site_refused", reply)
        start.assert_not_called()

    def test_the_consoles_own_same_origin_post_still_runs(self):
        # A fix that refuses the console's own UI is not a fix.
        self._ensure_server()
        reply = self.raw(self._same_origin_post("t.true"))
        self.assertTrue(reply.startswith(b"HTTP/1.1 202"), reply[:120])

    def test_a_post_with_no_sec_fetch_site_at_all_still_runs(self):
        # curl, and any non-browser client, sends no Sec-Fetch-* headers.
        # The second gate must only fire when the header is present and
        # says cross-site -- never on its absence, or it locks out every
        # client that is not a browser.
        self._ensure_server()
        payload = (
            f"POST /api/actions/t.true HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.port}\r\n"
            f"X-Health-Token: {self.cfg.token}\r\n"
            f"{ACTION_INTENT_HEADER}: {ACTION_INTENT_VALUE}\r\n"
            "Content-Length: 0\r\nConnection: close\r\n\r\n").encode("ascii")
        reply = self.raw(payload)
        self.assertTrue(reply.startswith(b"HTTP/1.1 202"), reply[:120])

    # --- the shutdown guard -------------------------------------------

    def test_a_post_after_shutdown_is_refused_and_starts_nothing(self):
        # cmd_run's finally block closes the listening socket, sets this
        # flag, shuts the runner down and closes the store. A keep-alive
        # connection a browser opened earlier can still deliver a request
        # into that window: without the guard it spawns a child nothing
        # will reap, whose audit write lands on a closed database. With
        # the real catalogue that orphan is apt-get running as root past
        # the console's own exit.
        self._ensure_server()
        self.server.shutdown_event.set()
        with mock.patch.object(self.runner, "start",
                               wraps=self.runner.start) as start:
            status, body = self.post("/api/actions/t.true")
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], "shutting_down")
        start.assert_not_called()

    # --- the request body ---------------------------------------------

    def test_a_body_is_not_left_in_the_socket_to_be_read_as_a_request(self):
        # do_POST reads no body, so Content-Length bytes left unread are
        # parsed as the next request line on a keep-alive connection. A
        # cross-origin <form enctype="text/plain"> controls those bytes
        # exactly, which turns one CSRF POST into two chosen actions.
        self._ensure_server()
        smuggled = (
            f"POST /api/actions/t.true HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.port}\r\n"
            f"X-Health-Token: {self.cfg.token}\r\n"
            f"{ACTION_INTENT_HEADER}: {ACTION_INTENT_VALUE}\r\n"
            "Content-Length: 0\r\n\r\n").encode("ascii")
        payload = (
            f"POST /api/actions/nope HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.port}\r\n"
            f"X-Health-Token: {self.cfg.token}\r\n"
            f"{ACTION_INTENT_HEADER}: {ACTION_INTENT_VALUE}\r\n"
            "Content-Type: text/plain\r\n"
            f"Content-Length: {len(smuggled)}\r\n"
            "\r\n").encode("ascii") + smuggled
        with mock.patch.object(self.runner, "start",
                               wraps=self.runner.start) as start:
            reply = self.raw(payload)
        self.assertNotIn(b"HTTP/1.1 202", reply)
        start.assert_not_called()

    def test_a_non_empty_body_is_refused_rather_than_ignored(self):
        self._ensure_server()
        payload = (
            f"POST /api/actions/t.true HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.port}\r\n"
            f"X-Health-Token: {self.cfg.token}\r\n"
            f"{ACTION_INTENT_HEADER}: {ACTION_INTENT_VALUE}\r\n"
            "Content-Length: 5\r\nConnection: close\r\n\r\nhello").encode("ascii")
        with mock.patch.object(self.runner, "start",
                               wraps=self.runner.start) as start:
            reply = self.raw(payload)
        self.assertTrue(reply.startswith(b"HTTP/1.1 400"), reply[:120])
        self.assertIn(b"body_refused", reply)
        start.assert_not_called()

    # --- the relay queue ------------------------------------------------

    def test_the_relay_never_drops_started_or_finished(self):
        # deque(maxlen=) evicts from the left regardless of what the entry
        # is, so a chatty run pushes its own "started" out and a second run
        # inside one tick can vanish from the stream entirely -- exactly
        # the loss ActionRunner._drop_one_locked was written to prevent.
        # Mirror its policy: only "output" is ever evicted.
        self._ensure_server()
        self.server.broadcast_action({"run_id": "r1", "phase": "started"})
        for index in range(ACTION_EVENT_QUEUE_MAX * 3):
            self.server.broadcast_action(
                {"run_id": "r1", "phase": "output", "line": str(index)})
        self.server.broadcast_action({"run_id": "r1", "phase": "finished"})
        phases = [event["phase"] for _, event in self.server.action_events]
        self.assertIn("started", phases)
        self.assertIn("finished", phases)
        self.assertLessEqual(len(self.server.action_events),
                             ACTION_EVENT_QUEUE_MAX)
        # And the client is told a gap happened rather than silently
        # served a stream with holes in it.
        self.assertGreater(self.server.action_relay_dropped[0], 0)

    def test_the_relay_keeps_both_runs_when_two_run_inside_one_tick(self):
        self._ensure_server()
        for run in ("r1", "r2"):
            self.server.broadcast_action({"run_id": run, "phase": "started"})
            for index in range(300):
                self.server.broadcast_action(
                    {"run_id": run, "phase": "output", "line": str(index)})
            self.server.broadcast_action({"run_id": run, "phase": "finished"})
        seen = {(event["run_id"], event["phase"])
                for _, event in self.server.action_events}
        for run in ("r1", "r2"):
            self.assertIn((run, "started"), seen)
            self.assertIn((run, "finished"), seen)

    # --- minors ---------------------------------------------------------

    def test_the_catalogue_route_lists_the_injected_catalogue(self):
        # Both routes must agree on which catalogue this server serves,
        # or a test catalogue lists apt.refresh on GET and refuses it on
        # POST.
        listed = {entry["id"] for entry in self.get_json("/api/actions")}
        self.assertEqual(listed, set(self.TEST_CATALOGUE))

    def test_an_unexpected_runner_failure_is_a_code_shaped_500(self):
        self._ensure_server()
        with mock.patch.object(self.runner, "start",
                               side_effect=RuntimeError("boom")):
            status, body = self.post("/api/actions/t.true")
        self.assertEqual(status, 500)
        self.assertEqual(body["error"], "action_failed")

    def test_a_refusal_still_hands_off_the_session_cookie(self):
        # A LAN client that authenticated with ?k= gets its cookie on the
        # 202 but not on any refusal, so the next request falls back to
        # the query string it was supposed to stop using.
        self._ensure_server()
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/actions/t.true"
            f"?k={self.cfg.token}", method="POST")
        request.add_header(ACTION_INTENT_HEADER, ACTION_INTENT_VALUE)
        with mock.patch("healthconsole.server.is_loopback",
                        return_value=False):
            try:
                response = urllib.request.urlopen(request, timeout=5)
                headers = response.headers
            except urllib.error.HTTPError as exc:
                headers = exc.headers
        self.assertIn("Set-Cookie", headers)

    def test_the_default_catalogue_cannot_be_mutated_through_the_seam(self):
        signature = inspect.signature(make_server)
        default = signature.parameters["catalogue"].default
        with self.assertRaises(TypeError):
            default["t.evil"] = None

class TestCmdRunShutdownOrdering(unittest.TestCase):
    """cmd_run's finally block -- the other half of the contract the
    server's shutdown_event guard depends on. It lives beside that guard
    rather than in test_cli.py because neither half means anything alone.
    """

    def test_the_store_is_closed_even_if_the_runner_shutdown_raises(self):
        # shutdown() cancels a run in flight and can raise (a child that
        # will not die, an OS error signalling it). The store must still
        # be closed: leaking the handle over a failure in the very step
        # that exists to make closing safe is the wrong trade.
        store, scheduler, server, runner = (mock.Mock() for _ in range(4))
        server.shutdown_event = threading.Event()
        runner.shutdown.side_effect = RuntimeError("a child that will not die")
        with mock.patch("healthconsole.cli._open",
                        return_value=(store, scheduler)), \
             mock.patch("healthconsole.cli.ActionRunner",
                        return_value=runner), \
             mock.patch("healthconsole.cli.make_server", return_value=server):
            with self.assertRaises(RuntimeError):
                cmd_run(Config(bind="127.0.0.1", port=0))
        runner.shutdown.assert_called_once()
        store.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
