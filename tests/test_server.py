import json
import socket
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from healthconsole.config import Config
from healthconsole.ring import Ring
from healthconsole.scheduler import Scheduler
from healthconsole.server import (
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
        # fall back to an HTML body.
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/now", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(ctx.exception.code, 501)
        self.assertIn("default-src 'self'",
                      ctx.exception.headers["Content-Security-Policy"])
        payload = json.loads(ctx.exception.read())
        self.assertIn("error", payload)

    def test_error_response_carries_connection_close(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/now", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(ctx.exception.headers["Connection"], "close")

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
        headers, _, body = raw.partition(b"\r\n\r\n")
        self.assertIn(b"Content-Length", headers)
        self.assertEqual(body, b"")


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


if __name__ == "__main__":
    unittest.main()
