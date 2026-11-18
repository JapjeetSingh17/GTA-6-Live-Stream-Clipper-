from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from gta6_clipper.app import CLIPS_DIR, app
from gta6_clipper.moments import (
    analyse_chunk_gemini,
    compute_file_hash,
    estimate_job,
    load_cached_moments,
    merge_moments,
    save_cached_moments,
)
from gta6_clipper.resolve_export import (
    export_moments,
    generate_csv,
    generate_edl,
    sec_to_tc,
    try_davinci_resolve_scripting,
)

client = TestClient(app, headers={"Host": "localhost:8000"})


class TestMomentsAnalyzer(unittest.TestCase):
    def setUp(self):
        CLIPS_DIR.mkdir(parents=True, exist_ok=True)

    def test_merge_moments_overlap_and_length_filtering(self):
        raw = [
            # Candidate 1: 10s-40s (30s), score 7
            {"start_sec": 10.0, "end_sec": 40.0, "score": 7, "title": "Good Drift", "reason": "Nice turn"},
            # Candidate 2: 20s-50s (30s), score 9 -> Overlaps Candidate 1 by 20s (66% > 50%), score 9 > 7 -> Kept!
            {"start_sec": 20.0, "end_sec": 50.0, "score": 9, "title": "Insane Drift", "reason": "Flawless cornering"},
            # Candidate 3: 60s-66s (6s) -> Dropped because < min_len (15s)
            {"start_sec": 60.0, "end_sec": 66.0, "score": 8, "title": "Quick Flash", "reason": "Too short"},
            # Candidate 4: 100s-200s (100s) -> Dropped because > max_len (60s)
            {"start_sec": 100.0, "end_sec": 200.0, "score": 10, "title": "Huge Cutscene", "reason": "Too long"},
            # Candidate 5: 70s-95s (25s), score 8 -> Distinct, kept!
            {"start_sec": 70.0, "end_sec": 95.0, "score": 8, "title": "Police Chase", "reason": "Helicopter pursuit"},
        ]

        merged = merge_moments(raw, min_len=15.0, max_len=60.0, max_moments=5)

        self.assertEqual(len(merged), 2)
        # Should keep Candidate 2 (score 9) and Candidate 5 (score 8)
        self.assertEqual(merged[0]["title"], "Insane Drift")
        self.assertEqual(merged[0]["score"], 9)
        self.assertEqual(merged[1]["title"], "Police Chase")
        self.assertEqual(merged[1]["score"], 8)
        # Verify chronological ordering by start_sec
        self.assertLess(merged[0]["start_sec"], merged[1]["start_sec"])

    def test_sidecar_cache_hit_and_invalidation(self):
        test_file = CLIPS_DIR / "test_sidecar_clip.mp4"
        test_file.write_bytes(b"dummy_video_bytes_for_hashing_1234567890")
        sidecar_file = CLIPS_DIR / "test_sidecar_clip.mp4.moments.json"

        params = {"max_moments": 5, "min_len": 15.0, "max_len": 60.0, "style_hint": None}
        moments_sample = [{"start_sec": 10.0, "end_sec": 30.0, "score": 9, "title": "Stunt", "reason": "Air time"}]

        try:
            save_cached_moments(test_file, "gemini-3.8-flash", params, moments_sample)
            self.assertTrue(sidecar_file.exists())

            # Cache hit
            hit = load_cached_moments(test_file, "gemini-3.8-flash", params)
            self.assertIsNotNone(hit)
            self.assertEqual(len(hit), 1)
            self.assertEqual(hit[0]["title"], "Stunt")

            # Cache miss on changed model
            miss_model = load_cached_moments(test_file, "gemini-2.5-pro", params)
            self.assertIsNone(miss_model)

            # Cache miss on changed parameters
            changed_params = dict(params, max_moments=10)
            miss_params = load_cached_moments(test_file, "gemini-3.8-flash", changed_params)
            self.assertIsNone(miss_params)

        finally:
            test_file.unlink(missing_ok=True)
            sidecar_file.unlink(missing_ok=True)

    def test_analyse_chunk_gemini_mocked_success_and_cleanup(self):
        mock_client = MagicMock()
        mock_file = MagicMock()
        mock_file.name = "files/test_mock_123"
        mock_file.state.value = "ACTIVE"

        mock_client.files.upload.return_value = mock_file
        mock_client.files.get.return_value = mock_file

        mock_resp = MagicMock()
        mock_resp.text = json.dumps({
            "moments": [
                {"start": "00:00:10", "end": "00:00:30", "score": 9, "title": "Rooftop Jump", "reason": "High altitude leap"}
            ]
        })
        mock_client.models.generate_content.return_value = mock_resp

        dummy_chunk = CLIPS_DIR / "dummy_chunk.mp4"
        dummy_chunk.write_bytes(b"dummy")

        try:
            results = analyse_chunk_gemini(mock_client, dummy_chunk, style_hint="Action")
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["title"], "Rooftop Jump")
            self.assertEqual(results[0]["score"], 9)

            # Confirm uploaded file was deleted in finally block
            mock_client.files.delete.assert_called_once_with(name="files/test_mock_123")
        finally:
            dummy_chunk.unlink(missing_ok=True)

    def test_analyse_chunk_gemini_bad_json_retry(self):
        mock_client = MagicMock()
        mock_file = MagicMock()
        mock_file.name = "files/retry_json_123"
        mock_file.state.value = "ACTIVE"
        mock_client.files.upload.return_value = mock_file
        mock_client.files.get.return_value = mock_file

        # First attempt returns invalid JSON, second attempt returns valid JSON
        bad_resp = MagicMock(text="Malformed JSON output without brackets")
        good_resp = MagicMock(text='{"moments": [{"start": "00:01:00", "end": "00:01:25", "score": 8, "title": "Car Chase", "reason": "Fast turn"}]}')
        mock_client.models.generate_content.side_effect = [bad_resp, good_resp]

        dummy_chunk = CLIPS_DIR / "dummy_retry_chunk.mp4"
        dummy_chunk.write_bytes(b"dummy")

        try:
            results = analyse_chunk_gemini(mock_client, dummy_chunk)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["title"], "Car Chase")
            self.assertEqual(mock_client.models.generate_content.call_count, 2)
            mock_client.files.delete.assert_called_once_with(name="files/retry_json_123")
        finally:
            dummy_chunk.unlink(missing_ok=True)

    def test_analyse_chunk_gemini_rate_limit_backoff(self):
        mock_client = MagicMock()
        mock_file = MagicMock()
        mock_file.name = "files/ratelimit_123"
        mock_file.state.value = "ACTIVE"
        mock_client.files.upload.return_value = mock_file
        mock_client.files.get.return_value = mock_file

        rate_limit_err = RuntimeError("429 Resource exhausted: Rate limit reached")
        good_resp = MagicMock(text='{"moments": [{"start": "00:00:15", "end": "00:00:40", "score": 8, "title": "Shootout", "reason": "Intense combat"}]}')
        mock_client.models.generate_content.side_effect = [rate_limit_err, good_resp]

        dummy_chunk = CLIPS_DIR / "dummy_rate_chunk.mp4"
        dummy_chunk.write_bytes(b"dummy")

        try:
            with patch("time.sleep", return_value=None):
                results = analyse_chunk_gemini(mock_client, dummy_chunk, max_retries=3)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["title"], "Shootout")
            self.assertEqual(mock_client.models.generate_content.call_count, 2)
        finally:
            dummy_chunk.unlink(missing_ok=True)

    def test_edl_and_csv_generation(self):
        moments = [
            {"start_sec": 15.0, "end_sec": 45.0, "score": 9, "title": "Grand Heist", "reason": "Vault breach"},
            {"start_sec": 90.0, "end_sec": 115.0, "score": 6, "title": "Alley Escape", "reason": "Motorcycle jump"},
        ]

        # Test EDL
        edl_text = generate_edl("test_video.mp4", moments, fps=30.0, timeline_offset="01:00:00:00")
        self.assertIn("TITLE: GTA6 AI Highlights - test_video.mp4", edl_text)
        self.assertIn("FCM: NON-DROP FRAME", edl_text)
        self.assertIn("001  AX       V     C", edl_text)
        # Score 9 -> GREEN marker
        self.assertIn("* MARKER: GREEN Grand Heist | Vault breach (Score: 9)", edl_text)
        # Score 6 -> YELLOW marker
        self.assertIn("* MARKER: YELLOW Alley Escape | Motorcycle jump (Score: 6)", edl_text)

        # Test CSV
        csv_text = generate_csv(moments)
        self.assertIn("Index,Start,End,StartSec,EndSec,DurationSec,Score,Color,Title,Reason", csv_text)
        self.assertIn("Grand Heist", csv_text)
        self.assertIn("Green", csv_text)
        self.assertIn("Yellow", csv_text)

    def test_resolve_fallback_when_unavailable(self):
        dummy_file = CLIPS_DIR / "dummy_resolve_clip.mp4"
        dummy_file.write_bytes(b"dummy_content")
        moments = [{"start_sec": 10.0, "end_sec": 30.0, "score": 9, "title": "Stunt", "reason": "Flip"}]

        try:
            res = export_moments(dummy_file, moments, fmt="resolve", output_dir=CLIPS_DIR)
            self.assertEqual(res["status"], "ok")
            self.assertEqual(res["format"], "resolve_fallback_edl")
            self.assertIn("Exported EDL and CSV", res["message"])
            self.assertTrue((CLIPS_DIR / res["edl_filename"]).exists())
            self.assertTrue((CLIPS_DIR / res["csv_filename"]).exists())
        finally:
            dummy_file.unlink(missing_ok=True)
            (CLIPS_DIR / "dummy_resolve_clip_highlights.edl").unlink(missing_ok=True)
            (CLIPS_DIR / "dummy_resolve_clip_highlights.csv").unlink(missing_ok=True)

    def test_security_path_traversal_on_all_endpoints(self):
        # Estimate endpoint
        r1 = client.post("/api/clip/moments/estimate", json={"filename": "../../etc/passwd.mp4"})
        self.assertEqual(r1.status_code, 400)

        # Moments analysis endpoint
        r2 = client.post("/api/clip/moments", json={"filename": "../../secret.mp4"})
        self.assertEqual(r2.status_code, 400)

        # Moments export endpoint
        r3 = client.post("/api/clip/moments/export", json={"filename": "../../secret.mp4", "format": "edl"})
        self.assertEqual(r3.status_code, 400)

    def test_no_secret_leak_in_api_responses(self):
        secret_key = "AIzaSySECRET_NEVER_LEAK_IN_LOGS_OR_HTTP"
        dummy_clip = CLIPS_DIR / "dummy_leak_test.mp4"
        dummy_clip.write_bytes(b"dummy")

        try:
            with patch.dict(os.environ, {"GEMINI_API_KEY": secret_key}):
                # Request a non-existent or failing flow
                r = client.post("/api/clip/moments/estimate", json={"filename": "dummy_leak_test.mp4"})
                self.assertNotIn(secret_key, r.text)

                r2 = client.get("/api/clip/moments/job_non_existent")
                self.assertNotIn(secret_key, r2.text)
        finally:
            dummy_clip.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
