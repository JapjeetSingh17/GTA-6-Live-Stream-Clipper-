"""
Tier 1 & Tier 2 Security Tests for GTA6-ZeroHour.
Covers:
- Host header enforcement (allowed vs rejected 403)
- Origin header enforcement (allowed vs rejected 403)
- Streamer ID regex validation (^[a-z0-9_-]{1,40}$)
- Stream URL domain whitelist validation
- Clip path traversal rejection
- Adversarial header spoofing and injection
"""

import re
import sys
from pathlib import Path
import unittest
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_harness import (
    raw_http_request,
    start_test_server,
    is_server_responding,
)

STREAMER_ID_REGEX = re.compile(r"^[a-z0-9_-]{1,40}$")
ALLOWED_DOMAINS = {"youtube.com", "youtu.be", "twitch.tv", "kick.com"}


def is_valid_streamer_id(sid: str) -> bool:
    return bool(STREAMER_ID_REGEX.match(sid))


def is_valid_stream_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        netloc = (parsed.netloc or "").lower().split(":")[0]
        if not netloc:
            return False
        return any(
            netloc == domain or netloc.endswith("." + domain)
            for domain in ALLOWED_DOMAINS
        )
    except Exception:
        return False


class TestSecurityFeatures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        start_test_server()

    # =========================================================================
    # 1. Host Header Enforcement (Tier 1 & Tier 2)
    # =========================================================================

    def test_allowed_host_localhost_8000(self):
        """Host: localhost:8000 must be accepted."""
        if not is_server_responding():
            self.skipTest("Server not running")
        resp = raw_http_request("GET", "/api/status", host="localhost:8000")
        self.assertEqual(
            resp.status,
            200,
            f"Expected 200 for Host: localhost:8000, got {resp.status}",
        )

    def test_allowed_host_127_0_0_1_8000(self):
        """Host: 127.0.0.1:8000 must be accepted."""
        if not is_server_responding():
            self.skipTest("Server not running")
        resp = raw_http_request("GET", "/api/status", host="127.0.0.1:8000")
        self.assertEqual(
            resp.status,
            200,
            f"Expected 200 for Host: 127.0.0.1:8000, got {resp.status}",
        )

    def test_allowed_host_localhost(self):
        """Host: localhost (without port) must be accepted."""
        if not is_server_responding():
            self.skipTest("Server not running")
        resp = raw_http_request("GET", "/api/status", host="localhost")
        self.assertEqual(
            resp.status,
            200,
            f"Expected 200 for Host: localhost, got {resp.status}",
        )

    def test_allowed_host_127_0_0_1(self):
        """Host: 127.0.0.1 (without port) must be accepted."""
        if not is_server_responding():
            self.skipTest("Server not running")
        resp = raw_http_request("GET", "/api/status", host="127.0.0.1")
        self.assertEqual(
            resp.status,
            200,
            f"Expected 200 for Host: 127.0.0.1, got {resp.status}",
        )

    def test_allowed_host_testserver(self):
        """Host: testserver (used by Starlette TestClient) must be accepted."""
        if not is_server_responding():
            self.skipTest("Server not running")
        resp = raw_http_request("GET", "/api/status", host="testserver")
        self.assertEqual(
            resp.status,
            200,
            f"Expected 200 for Host: testserver, got {resp.status}",
        )

    def test_rejected_host_evil_com(self):
        """Non-localhost Host: evil.com must return HTTP 403 Forbidden."""
        if not is_server_responding():
            self.skipTest("Server not running")
        resp = raw_http_request("GET", "/api/status", host="evil.com")
        self.assertEqual(
            resp.status,
            403,
            f"Expected 403 for Host: evil.com, got {resp.status}: {resp.text}",
        )

    def test_rejected_host_lan_ip(self):
        """Host with private LAN IP (192.168.1.100) must return HTTP 403 Forbidden."""
        if not is_server_responding():
            self.skipTest("Server not running")
        resp = raw_http_request(
            "GET", "/api/status", host="192.168.1.100:8000"
        )
        self.assertEqual(
            resp.status,
            403,
            f"Expected 403 for LAN IP Host, got {resp.status}",
        )

    def test_rejected_host_spoofed_subdomain(self):
        """Host: localhost.attacker.com must return HTTP 403 Forbidden."""
        if not is_server_responding():
            self.skipTest("Server not running")
        resp = raw_http_request(
            "GET", "/api/status", host="localhost.attacker.com"
        )
        self.assertEqual(
            resp.status,
            403,
            f"Expected 403 for spoofed host, got {resp.status}",
        )

    # =========================================================================
    # 2. Origin Header Enforcement (Tier 1 & Tier 2)
    # =========================================================================

    def test_allowed_origin_127_0_0_1_8000(self):
        """Origin: http://127.0.0.1:8000 must be accepted."""
        if not is_server_responding():
            self.skipTest("Server not running")
        resp = raw_http_request(
            "GET",
            "/api/status",
            headers={"Origin": "http://127.0.0.1:8000"},
            host="127.0.0.1:8000",
        )
        self.assertEqual(
            resp.status,
            200,
            f"Expected 200 for Origin: http://127.0.0.1:8000, got {resp.status}",
        )

    def test_allowed_origin_localhost_8000(self):
        """Origin: http://localhost:8000 must be accepted."""
        if not is_server_responding():
            self.skipTest("Server not running")
        resp = raw_http_request(
            "GET",
            "/api/status",
            headers={"Origin": "http://localhost:8000"},
            host="localhost:8000",
        )
        self.assertEqual(
            resp.status,
            200,
            f"Expected 200 for Origin: http://localhost:8000, got {resp.status}",
        )

    def test_allowed_missing_origin(self):
        """Missing Origin header (direct browser navigation or curl) must be accepted."""
        if not is_server_responding():
            self.skipTest("Server not running")
        resp = raw_http_request("GET", "/api/status", host="127.0.0.1:8000")
        self.assertEqual(
            resp.status, 200, f"Expected 200 without Origin, got {resp.status}"
        )

    def test_rejected_origin_evil_com(self):
        """Origin: http://evil.com must return HTTP 403 Forbidden."""
        if not is_server_responding():
            self.skipTest("Server not running")
        resp = raw_http_request(
            "GET",
            "/api/status",
            headers={"Origin": "http://evil.com"},
            host="127.0.0.1:8000",
        )
        self.assertEqual(
            resp.status,
            403,
            f"Expected 403 for Origin: evil.com, got {resp.status}",
        )

    def test_rejected_origin_null(self):
        """Origin: null (sandboxed iframe) must return HTTP 403 Forbidden."""
        if not is_server_responding():
            self.skipTest("Server not running")
        resp = raw_http_request(
            "GET",
            "/api/status",
            headers={"Origin": "null"},
            host="127.0.0.1:8000",
        )
        self.assertEqual(
            resp.status,
            403,
            f"Expected 403 for Origin: null, got {resp.status}",
        )

    def test_rejected_origin_lan_ip(self):
        """Origin: http://192.168.1.55:8000 must return HTTP 403 Forbidden."""
        if not is_server_responding():
            self.skipTest("Server not running")
        resp = raw_http_request(
            "GET",
            "/api/status",
            headers={"Origin": "http://192.168.1.55:8000"},
            host="127.0.0.1:8000",
        )
        self.assertEqual(
            resp.status,
            403,
            f"Expected 403 for LAN IP Origin, got {resp.status}",
        )

    # =========================================================================
    # 3. Streamer ID Regex Validation (Tier 1 & Tier 2)
    # Pattern: ^[a-z0-9_-]{1,40}$
    # =========================================================================

    def test_valid_streamer_ids_specification(self):
        """Valid streamer IDs must conform strictly to ^[a-z0-9_-]{1,40}$."""
        valid_ids = [
            "xqc",
            "kaicenat",
            "ishowspeed",
            "carry_is_live",
            "techno-gamerz_123",
            "a",
            "z" * 40,
            "streamer_01",
        ]
        for sid in valid_ids:
            with self.subTest(sid=sid):
                self.assertTrue(
                    is_valid_streamer_id(sid),
                    f"Expected valid streamer ID: '{sid}'",
                )

    def test_rejected_streamer_id_uppercase(self):
        """Uppercase letters are invalid in streamer IDs."""
        invalid_ids = ["KaiCenat", "IShowSpeed", "XQC", "StreamerOne"]
        for sid in invalid_ids:
            with self.subTest(sid=sid):
                self.assertFalse(
                    is_valid_streamer_id(sid),
                    f"Uppercase '{sid}' should be invalid",
                )

    def test_rejected_streamer_id_whitespace_and_special(self):
        """Spaces and non-alphanumeric punctuation are invalid."""
        invalid_ids = [
            "kai cenat",
            "streamer!",
            "user@kick",
            "user#1",
            "test$id",
        ]
        for sid in invalid_ids:
            with self.subTest(sid=sid):
                self.assertFalse(
                    is_valid_streamer_id(sid),
                    f"Punctuation/space in '{sid}' should be invalid",
                )

    def test_rejected_streamer_id_path_traversal(self):
        """Path traversal characters are invalid in streamer IDs."""
        invalid_ids = ["../admin", "ishowspeed/../root", "../../etc/passwd"]
        for sid in invalid_ids:
            with self.subTest(sid=sid):
                self.assertFalse(
                    is_valid_streamer_id(sid),
                    f"Traversal in '{sid}' should be invalid",
                )

    def test_rejected_streamer_id_length_bounds(self):
        """Empty strings and IDs longer than 40 chars are invalid."""
        self.assertFalse(
            is_valid_streamer_id(""), "Empty string should be invalid"
        )
        self.assertFalse(
            is_valid_streamer_id("a" * 41), "Length 41 should be invalid"
        )
        self.assertTrue(
            is_valid_streamer_id("a" * 40), "Length 40 should be valid"
        )

    def test_api_rejection_invalid_streamer_id(self):
        """POST /api/streamers must reject invalid streamer ID with HTTP 400 or 422."""
        if not is_server_responding():
            self.skipTest("Server not running")
        payload = {
            "id": "INVALID_ID_WITH_UPPERCASE",
            "name": "Invalid Streamer",
            "platform": "kick",
            "url": "https://kick.com/validstreamer",
            "enabled": True,
        }
        resp = raw_http_request("POST", "/api/streamers", body=payload)
        self.assertIn(
            resp.status,
            (400, 422),
            f"Expected 400 or 422 for invalid streamer ID, got {resp.status}: {resp.text}",
        )

    # =========================================================================
    # 4. Stream URL Domain Whitelist Validation (Tier 1 & Tier 2)
    # Whitelist: youtube.com, youtu.be, twitch.tv, kick.com
    # =========================================================================

    def test_valid_stream_urls_specification(self):
        """Allowed domain URLs must pass whitelist validation."""
        valid_urls = [
            "https://www.youtube.com/@IShowSpeed/live",
            "https://youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ",
            "https://twitch.tv/kaicenat",
            "https://www.twitch.tv/xqc",
            "https://kick.com/xqc",
            "http://kick.com/streamer",
        ]
        for url in valid_urls:
            with self.subTest(url=url):
                self.assertTrue(
                    is_valid_stream_url(url), f"Expected '{url}' to be valid"
                )

    def test_rejected_stream_url_unapproved_domains(self):
        """Unapproved external domains must be rejected."""
        invalid_urls = [
            "https://vimeo.com/123456",
            "https://dailymotion.com/video/x123",
            "https://evil.com/stream",
            "https://facebook.com/gaming",
        ]
        for url in invalid_urls:
            with self.subTest(url=url):
                self.assertFalse(
                    is_valid_stream_url(url),
                    f"Expected '{url}' to be rejected",
                )

    def test_rejected_stream_url_suffix_and_subdomain_spoofing(self):
        """Attacker domains mimicking allowed names must be rejected."""
        spoofed_urls = [
            "https://fakeyoutube.com/live",
            "https://youtube.com.evil.com/live",
            "https://notkick.com/channel",
            "https://twitch.tv.attacker.io/stream",
        ]
        for url in spoofed_urls:
            with self.subTest(url=url):
                self.assertFalse(
                    is_valid_stream_url(url),
                    f"Expected spoofed URL '{url}' to be rejected",
                )

    def test_rejected_stream_url_non_http_schemes(self):
        """Non-HTTP/HTTPS schemes (e.g. file, ftp) must be rejected."""
        dangerous_urls = [
            "file:///etc/passwd",
            "ftp://kick.com/malware.exe",
            "javascript:alert(1)",
        ]
        for url in dangerous_urls:
            with self.subTest(url=url):
                self.assertFalse(
                    is_valid_stream_url(url),
                    f"Scheme in '{url}' must be rejected",
                )

    def test_api_rejection_invalid_stream_url(self):
        """POST /api/streamers must reject unapproved stream URL domains with HTTP 400 or 422."""
        if not is_server_responding():
            self.skipTest("Server not running")
        payload = {
            "id": "unapproved_streamer",
            "name": "Unapproved Streamer",
            "platform": "vimeo",
            "url": "https://vimeo.com/channels/live",
            "enabled": True,
        }
        resp = raw_http_request("POST", "/api/streamers", body=payload)
        self.assertIn(
            resp.status,
            (400, 422),
            f"Expected 400 or 422 for unapproved domain, got {resp.status}: {resp.text}",
        )

    # =========================================================================
    # 5. Clip Path Traversal Guard (Tier 1 & Tier 2)
    # =========================================================================

    def test_rejected_clip_path_traversal_dotdot(self):
        """GET /clips/../../app.py must be rejected (HTTP 400 or 404, never 200)."""
        if not is_server_responding():
            self.skipTest("Server not running")
        resp = raw_http_request("GET", "/clips/../../app.py")
        self.assertNotEqual(
            resp.status,
            200,
            "Path traversal ../../app.py must never return HTTP 200",
        )
        self.assertIn(
            resp.status,
            (400, 404),
            f"Expected 400 or 404 for traversal, got {resp.status}",
        )

    def test_rejected_clip_path_non_mp4(self):
        """GET /clips/test.ts or other non-mp4 extensions must be rejected."""
        if not is_server_responding():
            self.skipTest("Server not running")
        resp = raw_http_request("GET", "/clips/secret.json")
        self.assertIn(
            resp.status,
            (400, 404),
            f"Expected 400 or 404 for non-mp4, got {resp.status}",
        )

    def test_rejected_clip_reveal_traversal(self):
        """POST /api/reveal with path traversal payload must return HTTP 400."""
        if not is_server_responding():
            self.skipTest("Server not running")
        resp = raw_http_request(
            "POST", "/api/reveal", body={"filename": "../../app.py"}
        )
        self.assertIn(
            resp.status,
            (400, 404),
            f"Expected 400 or 404 for reveal traversal, got {resp.status}: {resp.text}",
        )

    def test_rejected_clip_reveal_slashes(self):
        """POST /api/reveal with slash characters must return HTTP 400."""
        if not is_server_responding():
            self.skipTest("Server not running")
        resp = raw_http_request(
            "POST", "/api/reveal", body={"filename": "nested/dir/clip.mp4"}
        )
        self.assertIn(
            resp.status,
            (400, 404),
            f"Expected 400 or 404 for slashed filename, got {resp.status}",
        )


if __name__ == "__main__":
    unittest.main()
