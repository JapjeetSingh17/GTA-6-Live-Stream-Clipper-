"""
Authentication and Key Protection Tests for GTA6-ZeroHour.
Verifies single-key protection for:
- Login API endpoint (POST /api/auth/login)
- Cookie-based session generation and verification
- Header-based API authentication (X-Access-Key)
- Unauthenticated API and video blocking (HTTP 401)
- Root UI delivery: unauthenticated -> login.html, authenticated -> index.html
- Logout endpoint (POST /api/auth/logout)
"""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_harness import (
    start_test_server,
    is_server_responding,
    raw_http_request,
)

CORRECT_KEY = "Javelloui*~*.Yrs->2004"
WRONG_KEY = "WrongKey12345!"


class TestAuth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        start_test_server()

    def test_login_success_sets_session_cookie(self):
        """Valid key must return HTTP 200 and Set-Cookie with gta6_session."""
        if not is_server_responding():
            self.skipTest("Server not running")
        resp = raw_http_request(
            "POST",
            "/api/auth/login",
            body={"key": CORRECT_KEY},
            headers={"X-No-Auth": "1"},
        )
        self.assertEqual(resp.status, 200)
        data = resp.json()
        self.assertEqual(data.get("status"), "ok")
        self.assertIn("token", data)

        set_cookie = resp.get_header("set-cookie") or ""
        self.assertIn("gta6_session=", set_cookie)

    def test_login_failure_with_wrong_key(self):
        """Invalid key must return HTTP 401."""
        if not is_server_responding():
            self.skipTest("Server not running")
        resp = raw_http_request(
            "POST",
            "/api/auth/login",
            body={"key": WRONG_KEY},
            headers={"X-No-Auth": "1"},
        )
        self.assertEqual(resp.status, 401)
        data = resp.json()
        self.assertIn("detail", data)

    def test_login_failure_with_empty_key(self):
        """Empty key must return HTTP 401."""
        if not is_server_responding():
            self.skipTest("Server not running")
        resp = raw_http_request(
            "POST",
            "/api/auth/login",
            body={"key": ""},
            headers={"X-No-Auth": "1"},
        )
        self.assertEqual(resp.status, 401)

    def test_unauthenticated_api_request_rejected(self):
        """API endpoints must return HTTP 401 when request is unauthenticated."""
        if not is_server_responding():
            self.skipTest("Server not running")
        resp = raw_http_request(
            "GET",
            "/api/status",
            headers={"X-No-Auth": "1"},
        )
        self.assertEqual(resp.status, 401)
        data = resp.json()
        self.assertIn("detail", data)

    def test_authenticated_api_request_via_header(self):
        """API endpoints must accept X-Access-Key header."""
        if not is_server_responding():
            self.skipTest("Server not running")
        resp = raw_http_request(
            "GET",
            "/api/status",
            headers={"X-Access-Key": CORRECT_KEY},
        )
        self.assertEqual(resp.status, 200)

    def test_authenticated_api_request_via_session_cookie(self):
        """API endpoints must accept gta6_session cookie obtained from login."""
        if not is_server_responding():
            self.skipTest("Server not running")
        # 1. Login to get token
        login_resp = raw_http_request(
            "POST",
            "/api/auth/login",
            body={"key": CORRECT_KEY},
            headers={"X-No-Auth": "1"},
        )
        token = login_resp.json().get("token")
        self.assertTrue(token, "Login token missing")

        # 2. Call API with session cookie
        resp = raw_http_request(
            "GET",
            "/api/status",
            headers={"Cookie": f"gta6_session={token}"},
        )
        self.assertEqual(resp.status, 200)

    def test_unauthenticated_root_serves_login_page(self):
        """Unauthenticated browser visits to GET / must serve the login page."""
        if not is_server_responding():
            self.skipTest("Server not running")
        resp = raw_http_request(
            "GET",
            "/",
            headers={"X-No-Auth": "1"},
        )
        self.assertEqual(resp.status, 200)
        self.assertIn("text/html", resp.get_header("Content-Type") or "")
        self.assertIn("RESTRICTED ACCESS", resp.text)
        self.assertIn("access-key", resp.text)

    def test_authenticated_root_serves_dashboard(self):
        """Authenticated requests to GET / must serve the GTA6-ZeroHour dashboard."""
        if not is_server_responding():
            self.skipTest("Server not running")
        resp = raw_http_request(
            "GET",
            "/",
            headers={"X-Access-Key": CORRECT_KEY},
        )
        self.assertEqual(resp.status, 200)
        self.assertIn("text/html", resp.get_header("Content-Type") or "")
        self.assertIn("Live Channels & Buffer Feeds", resp.text)

    def test_logout_endpoint(self):
        """POST /api/auth/logout must return HTTP 200."""
        if not is_server_responding():
            self.skipTest("Server not running")
        resp = raw_http_request(
            "POST",
            "/api/auth/logout",
            headers={"X-Access-Key": CORRECT_KEY},
        )
        self.assertEqual(resp.status, 200)


if __name__ == "__main__":
    unittest.main()
