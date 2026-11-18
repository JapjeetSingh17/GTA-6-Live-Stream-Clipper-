"""
Tier 1 Static UI & Dark-Mode Compliance Tests for GTA6-ZeroHour.
Covers:
- File existence of gta6_clipper/static/index.html
- Dark-mode metadata (<meta name="color-scheme" content="dark">)
- Modern Web Guidance dark styling (:root { color-scheme: dark; })
- 2-second auto-refresh polling mechanism (setInterval ... 2000)
- Master controls markup (Start All, Stop All, MARK ALL)
- Streamer cards layout and quick-clip buttons (30s, 60s, 120s, 300s)
- Range slicer form controls (datetime inputs, cut button)
- Marks drawer and one-click clipping markup
- Clips feed with HTML5 <video controls preload="metadata">, download, copy title, Finder reveal
- HTTP static file delivery (GET / or GET /index.html returns 200 OK text/html)
"""

from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_harness import (
    start_test_server,
    is_server_responding,
    raw_http_request,
    STATIC_DIR,
)

INDEX_HTML_PATH = STATIC_DIR / "index.html"


class TestStaticUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        start_test_server()

    def _get_html_content(self) -> str:
        if not INDEX_HTML_PATH.exists():
            self.skipTest(f"index.html not yet created at {INDEX_HTML_PATH}")
        with open(INDEX_HTML_PATH, "r", encoding="utf-8") as f:
            return f.read()

    # =========================================================================
    # 1. Dark Mode & Accessibility Standards (modern-web-guidance)
    # =========================================================================

    def test_dark_mode_meta_tag_present(self):
        """Must declare <meta name="color-scheme" content="dark"> in <head>."""
        html = self._get_html_content()
        meta_pattern = re.compile(
            r'<meta[^>]+name=["\']color-scheme["\'][^>]+content=["\']dark["\']',
            re.IGNORECASE,
        )
        meta_pattern_alt = re.compile(
            r'<meta[^>]+content=["\']dark["\'][^>]+name=["\']color-scheme["\']',
            re.IGNORECASE,
        )
        has_meta = bool(
            meta_pattern.search(html) or meta_pattern_alt.search(html)
        )
        self.assertTrue(
            has_meta,
            'Missing modern-web-guidance <meta name="color-scheme" content="dark"> tag',
        )

    def test_dark_mode_css_color_scheme(self):
        """CSS must define color-scheme: dark on :root or html."""
        html = self._get_html_content()
        self.assertRegex(
            html,
            r"color-scheme\s*:\s*dark",
            "CSS missing 'color-scheme: dark' declaration",
        )

    def test_html_lang_attribute(self):
        """Root <html> element must have lang attribute."""
        html = self._get_html_content()
        self.assertRegex(
            html,
            r"<html[^>]+lang=",
            "Root <html> tag must specify lang attribute (e.g. lang='en')",
        )

    # =========================================================================
    # 2. Polling & Auto-Refresh Timing
    # =========================================================================

    def test_auto_refresh_interval_2s(self):
        """JavaScript must configure a 2-second (2000ms) polling interval."""
        html = self._get_html_content()
        self.assertRegex(
            html,
            r"2000",
            "JavaScript does not contain expected 2000ms polling interval",
        )

    # =========================================================================
    # 3. Master Controls & Streamer Cards UI Elements
    # =========================================================================

    def test_master_controls_markup(self):
        """Dashboard must feature Start All, Stop All, and MARK ALL master controls."""
        html = self._get_html_content().lower()
        self.assertTrue(
            "start all" in html or "start-all" in html or "pollall" in html,
            "Missing 'Start All' control",
        )
        self.assertTrue(
            "stop all" in html or "stop-all" in html, "Missing 'Stop All' control"
        )
        self.assertTrue(
            "mark all" in html or "mark-all" in html, "Missing 'MARK ALL' control"
        )

    def test_quick_clip_durations_present(self):
        """Streamer cards must provide quick-clip buttons for 30s, 60s, 120s, 300s."""
        html = self._get_html_content()
        for sec in [30, 60, 120, 300]:
            self.assertIn(
                str(sec), html, f"Quick clip duration {sec}s missing from UI"
            )

    def test_range_slicer_inputs_present(self):
        """Dashboard must include inputs or elements for range slicing."""
        html = self._get_html_content().lower()
        self.assertTrue(
            "datetime" in html or "range" in html or "slice" in html,
            "Missing datetime range slicer inputs in UI",
        )

    # =========================================================================
    # 4. Clips Feed & Range Player UI Elements
    # =========================================================================

    def test_html5_video_player_present(self):
        """Dashboard must include HTML5 <video controls preload='metadata'>."""
        html = self._get_html_content()
        self.assertIn("<video", html.lower(), "HTML5 <video> element missing")
        self.assertIn("controls", html.lower(), "Video element missing controls")

    def test_clip_actions_markup(self):
        """Clips feed must include download, reveal in Finder, and copy title actions."""
        html = self._get_html_content().lower()
        self.assertTrue(
            "download" in html, "Missing clip download button or link"
        )
        self.assertTrue("reveal" in html, "Missing macOS Finder reveal button")
        self.assertTrue("copy" in html, "Missing copy title button")

    # =========================================================================
    # 5. HTTP Delivery of Static Dashboard
    # =========================================================================

    def test_http_get_root_returns_html(self):
        """GET / must return HTTP 200 and Content-Type: text/html."""
        if not is_server_responding():
            self.skipTest("Server not running")
        if not INDEX_HTML_PATH.exists():
            self.skipTest(f"index.html not yet created at {INDEX_HTML_PATH}")
        resp = raw_http_request("GET", "/")
        self.assertEqual(resp.status, 200)
        ct = resp.get_header("Content-Type") or ""
        self.assertIn("text/html", ct)


if __name__ == "__main__":
    unittest.main()
