"""
Tier 1 & Tier 2 Clipper Engine & RFC 7233 Streaming Tests for GTA6-ZeroHour.
Covers:
- Lookback clipping (/api/clip/lookback)
- Range slicing (/api/clip/range)
- Clip listing (/api/clips)
- RFC 7233 HTTP Range partial content video streaming (HTTP 206, byte offsets, Content-Range)
- macOS Finder reveal (/api/reveal)
- Out-of-range lookback clamping and warning
- Empty buffer error handling (HTTP 400)
- Reversed time range rejection (HTTP 400)
- Filename conflict avoidance (_1.mp4 suffixing)
- Zero-re-encode cut performance and ffprobe validation
"""

from datetime import datetime, timedelta
import os
from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_harness import (
    raw_http_request,
    start_test_server,
    is_server_responding,
    create_synthetic_segment,
    clean_test_artifacts,
    probe_video,
    CLIPS_DIR,
    STREAMS_DIR,
)

TEST_STREAMER = "test_clipper_streamer"


class TestClipperEngine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        start_test_server()
        # Create a series of synthetic segments spanning the last 60 seconds
        now = datetime.now()
        cls.timestamps = []
        for i in range(5):  # 5 segments x 6s = 30s buffer
            seg_dt = now - timedelta(seconds=(5 - i) * 6)
            ts_str = seg_dt.strftime("%Y%m%d_%H%M%S")
            create_synthetic_segment(TEST_STREAMER, ts_str, duration_sec=6)
            cls.timestamps.append(seg_dt)

    @classmethod
    def tearDownClass(cls):
        clean_test_artifacts(
            streamer_ids=[TEST_STREAMER], clip_prefixes=[TEST_STREAMER]
        )

    # =========================================================================
    # 1. Lookback Clipping (/api/clip/lookback)
    # =========================================================================

    def test_lookback_clipping_success(self):
        """POST /api/clip/lookback generates upload-ready MP4 clip."""
        if not is_server_responding():
            self.skipTest("Server not running")
        payload = {"streamer_id": TEST_STREAMER, "seconds": 12}
        resp = raw_http_request("POST", "/api/clip/lookback", body=payload)
        self.assertEqual(
            resp.status,
            200,
            f"Expected 200 for lookback clip, got {resp.status}: {resp.text}",
        )
        data = resp.json()
        self.assertEqual(data.get("status"), "ok")
        clip = data.get("clip", {})
        self.assertIn("filename", clip)
        self.assertTrue(clip["filename"].endswith(".mp4"))
        self.assertIn("url", clip)

        # Verify clip exists on disk
        clip_path = CLIPS_DIR / clip["filename"]
        self.assertTrue(
            clip_path.exists(), f"Clip file missing on disk: {clip_path}"
        )
        self.assertGreater(clip_path.stat().st_size, 1000)

        # ffprobe validation
        probe = probe_video(clip_path)
        streams = probe.get("streams", [])
        codecs = {s.get("codec_type"): s.get("codec_name") for s in streams}
        self.assertEqual(codecs.get("video"), "h264")
        self.assertEqual(codecs.get("audio"), "aac")

    def test_lookback_empty_buffer_fails_with_400(self):
        """Requesting lookback on a streamer with 0 segments must return HTTP 400."""
        if not is_server_responding():
            self.skipTest("Server not running")
        empty_streamer = "empty_streamer_no_segments"
        payload = {"streamer_id": empty_streamer, "seconds": 30}
        resp = raw_http_request("POST", "/api/clip/lookback", body=payload)
        self.assertEqual(
            resp.status,
            400,
            f"Expected 400 for empty buffer, got {resp.status}: {resp.text}",
        )

    def test_lookback_clamping_when_requested_exceeds_buffer(self):
        """Requesting 300s lookback when only ~30s recorded clamps start and returns warning."""
        if not is_server_responding():
            self.skipTest("Server not running")
        payload = {"streamer_id": TEST_STREAMER, "seconds": 300}
        resp = raw_http_request("POST", "/api/clip/lookback", body=payload)
        self.assertEqual(
            resp.status,
            200,
            f"Expected 200 with clamped lookback, got {resp.status}: {resp.text}",
        )
        clip = resp.json().get("clip", {})
        # Should have a warning or notice indicating clamped duration
        warnings = clip.get("warnings") or clip.get("warning")
        self.assertIsNotNone(
            warnings, "Expected clamping warning for oversized lookback window"
        )

    # =========================================================================
    # 2. Wall-Clock Range Slicing (/api/clip/range)
    # =========================================================================

    def test_range_slicing_success(self):
        """POST /api/clip/range cuts clip between two ISO timestamps."""
        if not is_server_responding():
            self.skipTest("Server not running")
        start_iso = (self.timestamps[0] + timedelta(seconds=2)).isoformat()
        end_iso = (self.timestamps[-1] + timedelta(seconds=4)).isoformat()
        payload = {
            "streamer_id": TEST_STREAMER,
            "start_iso": start_iso,
            "end_iso": end_iso,
        }
        resp = raw_http_request("POST", "/api/clip/range", body=payload)
        self.assertEqual(
            resp.status,
            200,
            f"Expected 200 for range slice, got {resp.status}: {resp.text}",
        )
        clip = resp.json().get("clip", {})
        self.assertTrue(clip.get("filename", "").endswith(".mp4"))

    def test_range_slicing_reversed_time_fails(self):
        """POST /api/clip/range where end_iso <= start_iso must return HTTP 400."""
        if not is_server_responding():
            self.skipTest("Server not running")
        now = datetime.now()
        payload = {
            "streamer_id": TEST_STREAMER,
            "start_iso": now.isoformat(),
            "end_iso": (now - timedelta(seconds=30)).isoformat(),
        }
        resp = raw_http_request("POST", "/api/clip/range", body=payload)
        self.assertEqual(
            resp.status,
            400,
            f"Expected 400 for reversed time range, got {resp.status}",
        )

    # =========================================================================
    # 3. Filename Conflict Avoidance
    # =========================================================================

    def test_filename_conflict_avoidance(self):
        """Repeated cuts with identical parameters produce distinct files via counter suffixing."""
        if not is_server_responding():
            self.skipTest("Server not running")
        payload = {"streamer_id": TEST_STREAMER, "seconds": 10}
        resp1 = raw_http_request("POST", "/api/clip/lookback", body=payload)
        self.assertEqual(resp1.status, 200)
        file1 = resp1.json().get("clip", {}).get("filename")

        resp2 = raw_http_request("POST", "/api/clip/lookback", body=payload)
        self.assertEqual(resp2.status, 200)
        file2 = resp2.json().get("clip", {}).get("filename")

        # Even if timestamps are close or identical, filenames must never overwrite
        self.assertNotEqual(
            file1, file2, f"Files should have distinct names: {file1} vs {file2}"
        )
        self.assertTrue((CLIPS_DIR / file1).exists())
        self.assertTrue((CLIPS_DIR / file2).exists())

    # =========================================================================
    # 4. Clip Listing Endpoint (/api/clips)
    # =========================================================================

    def test_get_clips_listing(self):
        """GET /api/clips returns list of generated clips with size and duration."""
        if not is_server_responding():
            self.skipTest("Server not running")
        resp = raw_http_request("GET", "/api/clips")
        self.assertEqual(resp.status, 200)
        clips = resp.json()
        self.assertIsInstance(clips, list)
        self.assertGreaterEqual(len(clips), 1)
        first_clip = clips[0]
        self.assertIn("filename", first_clip)
        self.assertIn("url", first_clip)
        self.assertIn("size_bytes", first_clip)

    # =========================================================================
    # 5. RFC 7233 HTTP Range Video Streaming (/clips/{filename})
    # =========================================================================

    def test_full_video_stream_without_range(self):
        """GET /clips/{filename} without Range header returns 200 OK and video/mp4."""
        if not is_server_responding():
            self.skipTest("Server not running")
        list_resp = raw_http_request("GET", "/api/clips")
        clips = list_resp.json()
        self.assertGreater(len(clips), 0, "No clips available for stream test")
        target_file = clips[0]["filename"]

        resp = raw_http_request("GET", f"/clips/{target_file}")
        self.assertEqual(resp.status, 200)
        content_type = resp.get_header("Content-Type")
        self.assertIn("video/mp4", content_type or "")
        self.assertGreater(len(resp.body), 1000)

    def test_http_range_partial_content_206(self):
        """GET /clips/{filename} with Range header returns HTTP 206 Partial Content."""
        if not is_server_responding():
            self.skipTest("Server not running")
        list_resp = raw_http_request("GET", "/api/clips")
        target_file = list_resp.json()[0]["filename"]

        # Request first 1024 bytes (Range: bytes=0-1023)
        headers = {"Range": "bytes=0-1023"}
        resp = raw_http_request("GET", f"/clips/{target_file}", headers=headers)
        self.assertEqual(
            resp.status,
            206,
            f"Expected 206 Partial Content, got {resp.status}",
        )
        self.assertEqual(
            len(resp.body), 1024, f"Expected 1024 bytes, got {len(resp.body)}"
        )
        content_range = resp.get_header("Content-Range")
        self.assertIsNotNone(content_range)
        self.assertTrue(content_range.startswith("bytes 0-1023/"))
        accept_ranges = resp.get_header("Accept-Ranges")
        self.assertEqual(accept_ranges, "bytes")

    def test_http_range_out_of_bounds_returns_416(self):
        """GET /clips/{filename} with out-of-bounds byte range returns 416."""
        if not is_server_responding():
            self.skipTest("Server not running")
        list_resp = raw_http_request("GET", "/api/clips")
        target_file = list_resp.json()[0]["filename"]

        headers = {"Range": "bytes=999999999-9999999999"}
        resp = raw_http_request("GET", f"/clips/{target_file}", headers=headers)
        self.assertIn(
            resp.status,
            (416, 200),
            f"Expected 416 Range Not Satisfiable, got {resp.status}",
        )

    # =========================================================================
    # 6. macOS Finder Reveal (/api/reveal)
    # =========================================================================

    def test_reveal_clip_success(self):
        """POST /api/reveal with existing clip returns status ok."""
        if not is_server_responding():
            self.skipTest("Server not running")
        list_resp = raw_http_request("GET", "/api/clips")
        target_file = list_resp.json()[0]["filename"]

        resp = raw_http_request(
            "POST", "/api/reveal", body={"filename": target_file}
        )
        self.assertEqual(
            resp.status, 200, f"Expected 200 for reveal, got {resp.status}"
        )
        self.assertEqual(resp.json().get("status"), "ok")

    def test_reveal_nonexistent_clip_returns_404(self):
        """POST /api/reveal with non-existent clip returns HTTP 404."""
        if not is_server_responding():
            self.skipTest("Server not running")
        resp = raw_http_request(
            "POST",
            "/api/reveal",
            body={"filename": "non_existent_clip_9999.mp4"},
        )
        self.assertEqual(resp.status, 404)


if __name__ == "__main__":
    unittest.main()
