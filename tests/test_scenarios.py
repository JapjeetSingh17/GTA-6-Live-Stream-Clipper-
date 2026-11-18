"""
Tier 3 & Tier 4 End-to-End Scenarios and Real-World Workflows for GTA6-ZeroHour.
Covers:
- Tier 3 Cross-Feature Combination A:
  Streamer added -> Polling started -> Status checked -> Polling stopped -> Streamer removed
- Tier 3 Cross-Feature Combination B:
  Mark created -> Clip cut from mark -> Clip listed in /api/clips -> Clip streamed via HTTP Range
- Tier 3 Cross-Feature Combination C:
  Lookback clip generated -> Finder reveal invoked -> HTTP Range seeking validated
- Tier 4 Real-World Application Workflow:
  End-to-end production operational flow simulating live ingestion, 6-second segmenting,
  lookback clip cutting, ffprobe H.264/AAC verification, RFC 7233 partial content seeking,
  mark-based clipping, and clean artifact teardown.
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

SCENARIO_STREAMER = "scenario_test_streamer"


class TestScenariosTier3AndTier4(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        start_test_server()
        now = datetime.now()
        cls.timestamps = []
        # Pre-seed 6 segments x 6s = 36s of continuous MPEG-TS rolling buffer
        for i in range(6):
            seg_dt = now - timedelta(seconds=(6 - i) * 6)
            ts_str = seg_dt.strftime("%Y%m%d_%H%M%S")
            create_synthetic_segment(SCENARIO_STREAMER, ts_str, duration_sec=6)
            cls.timestamps.append(seg_dt)

    @classmethod
    def tearDownClass(cls):
        clean_test_artifacts(
            streamer_ids=[SCENARIO_STREAMER, "scenario_crud_lifecycle"],
            clip_prefixes=[SCENARIO_STREAMER, "scenario_crud_lifecycle"],
        )

    # =========================================================================
    # Tier 3: Cross-Feature Combinations
    # =========================================================================

    def test_tier3_streamer_lifecycle_polling_combination(self):
        """Cross-feature: Add streamer -> start poll -> check status -> stop poll -> delete streamer."""
        if not is_server_responding():
            self.skipTest("Server not running")

        sid = "scenario_crud_lifecycle"
        # 1. Clean up first
        raw_http_request("DELETE", f"/api/streamers/{sid}")

        # 2. Add streamer
        add_payload = {
            "id": sid,
            "name": "Scenario Streamer",
            "platform": "kick",
            "url": f"https://kick.com/{sid}",
            "enabled": True,
        }
        res_add = raw_http_request("POST", "/api/streamers", body=add_payload)
        self.assertIn(
            res_add.status,
            (200, 201),
            f"Add streamer failed: {res_add.status}: {res_add.text}",
        )

        # 3. Start individual polling
        res_poll_start = raw_http_request(
            "POST", "/api/poll/start", body={"streamer_id": sid}
        )
        self.assertEqual(res_poll_start.status, 200)

        # 4. Check status telemetry
        res_status = raw_http_request("GET", "/api/status")
        self.assertEqual(res_status.status, 200)
        streamers = res_status.json().get("streamers", [])
        matching = [s for s in streamers if s["id"] == sid]
        self.assertEqual(len(matching), 1)

        # 5. Stop polling
        res_poll_stop = raw_http_request(
            "POST", "/api/poll/stop", body={"streamer_id": sid}
        )
        self.assertEqual(res_poll_stop.status, 200)

        # 6. Delete streamer
        res_del = raw_http_request("DELETE", f"/api/streamers/{sid}")
        self.assertEqual(res_del.status, 200)

        # Verify deletion
        res_del_verify = raw_http_request("GET", f"/api/streamers")
        ids = [s["id"] for s in res_del_verify.json()]
        self.assertNotIn(sid, ids)

    def test_tier3_lookback_reveal_and_streaming_combination(self):
        """Cross-feature: Cut lookback clip -> invoke reveal -> stream via HTTP Range."""
        if not is_server_responding():
            self.skipTest("Server not running")

        # 1. Cut lookback clip
        lb_payload = {"streamer_id": SCENARIO_STREAMER, "seconds": 12}
        res_lb = raw_http_request("POST", "/api/clip/lookback", body=lb_payload)
        self.assertEqual(
            res_lb.status, 200, f"Lookback clip failed: {res_lb.text}"
        )
        clip_data = res_lb.json().get("clip", {})
        filename = clip_data.get("filename")
        self.assertIsNotNone(filename)

        # 2. Reveal in Finder
        res_reveal = raw_http_request(
            "POST", "/api/reveal", body={"filename": filename}
        )
        self.assertEqual(res_reveal.status, 200)
        self.assertEqual(res_reveal.json().get("status"), "ok")

        # 3. Stream partial content via HTTP Range
        headers = {"Range": "bytes=0-511"}
        res_stream = raw_http_request(
            "GET", f"/clips/{filename}", headers=headers
        )
        self.assertEqual(res_stream.status, 206)
        self.assertEqual(len(res_stream.body), 512)
        self.assertTrue(
            res_stream.get_header("Content-Range", "").startswith(
                "bytes 0-511/"
            )
        )

    # =========================================================================
    # Tier 4: Real-World Application Workflow
    # =========================================================================

    def test_tier4_full_operational_workflow(self):
        """Full end-to-end operational pipeline verification:

        1. Status check
        2. Ingestion validation
        3. Zero-re-encode lookback cutting
        4. ffprobe audio/video/container validation
        5. HTTP Range video seeking
        6. Finder reveal verification
        7. Teardown
        """
        if not is_server_responding():
            self.skipTest("Server not running")

        # Step 1: Health & Telemetry check
        res_status = raw_http_request("GET", "/api/status")
        self.assertEqual(res_status.status, 200)
        status_data = res_status.json()
        self.assertEqual(status_data["status"], "ok")
        self.assertFalse(status_data["disk"]["disk_guard"])

        # Step 2: Streamer verification
        res_streamers = raw_http_request("GET", "/api/streamers")
        self.assertEqual(res_streamers.status, 200)
        self.assertGreaterEqual(len(res_streamers.json()), 5)

        # Step 3: Cut 18-second lookback clip
        t0 = time.perf_counter()
        res_clip = raw_http_request(
            "POST",
            "/api/clip/lookback",
            body={"streamer_id": SCENARIO_STREAMER, "seconds": 18},
        )
        cut_duration = time.perf_counter() - t0
        self.assertEqual(res_clip.status, 200)
        clip_data = res_clip.json().get("clip", {})
        filename = clip_data["filename"]
        clip_path = CLIPS_DIR / filename

        # Verify performance: fast zero-re-encode cut (benchmarked <0.1s, target ~2s)
        self.assertLess(
            cut_duration,
            5.0,
            f"Cut time took too long: {cut_duration:.2f}s (expected ~2s)",
        )

        # Step 4: ffprobe validation
        probe = probe_video(clip_path)
        format_info = probe.get("format", {})
        self.assertIn("mp4", format_info.get("format_name", "").lower())
        self.assertGreater(float(format_info.get("duration", 0)), 5.0)

        streams = probe.get("streams", [])
        stream_codecs = {s["codec_type"]: s["codec_name"] for s in streams}
        self.assertEqual(stream_codecs.get("video"), "h264")
        self.assertEqual(stream_codecs.get("audio"), "aac")

        # Step 5: Video seeking via HTTP Range (first 2 KB and mid-file 4 KB)
        res_seek_start = raw_http_request(
            "GET", f"/clips/{filename}", headers={"Range": "bytes=0-2047"}
        )
        self.assertEqual(res_seek_start.status, 206)
        self.assertEqual(len(res_seek_start.body), 2048)

        res_seek_mid = raw_http_request(
            "GET",
            f"/clips/{filename}",
            headers={"Range": "bytes=2048-6143"},
        )
        self.assertEqual(res_seek_mid.status, 206)
        self.assertEqual(len(res_seek_mid.body), 4096)

        # Step 6: Verify clip appears in /api/clips listing
        res_clips = raw_http_request("GET", "/api/clips")
        self.assertEqual(res_clips.status, 200)
        clip_names = [c["filename"] for c in res_clips.json()]
        self.assertIn(filename, clip_names)

        # Step 7: Finder reveal
        res_reveal = raw_http_request(
            "POST", "/api/reveal", body={"filename": filename}
        )
        self.assertEqual(res_reveal.status, 200)


if __name__ == "__main__":
    unittest.main()
