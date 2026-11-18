import json
import math
import os
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from gta6_clipper.app import CLIPS_DIR, app
from gta6_clipper.extractor import (
    format_seconds_to_hms,
    get_media_duration,
    parse_time_offset,
)

client = TestClient(app, headers={"Host": "localhost:8000", "X-Access-Key": "Javelloui*~*.Yrs->2004"})

class TestExtractorAndTrimmer(unittest.TestCase):
    def test_parse_time_offset_valid_formats(self):
        self.assertEqual(parse_time_offset("01:23:45"), 5025.0)
        self.assertEqual(parse_time_offset("15:30"), 930.0)
        self.assertEqual(parse_time_offset("10m"), 600.0)
        self.assertEqual(parse_time_offset("+10m"), 600.0)
        self.assertEqual(parse_time_offset("45s"), 45.0)
        self.assertEqual(parse_time_offset("1.5h"), 5400.0)
        self.assertEqual(parse_time_offset(120), 120.0)
        self.assertEqual(parse_time_offset("120.5"), 120.5)

    def test_parse_time_offset_reject_invalid(self):
        invalid_cases = [
            "-10",
            "-01:00:00",
            "",
            "   ",
            None,
            "abc",
            "12:34:56:78",
            "12:99",  # seconds >= 60 in MM:SS
            float("inf"),
            float("-inf"),
            float("nan"),
        ]
        for c in invalid_cases:
            with self.subTest(case=c):
                with self.assertRaises(ValueError):
                    parse_time_offset(c)

    def test_format_seconds_to_hms(self):
        self.assertEqual(format_seconds_to_hms(5025), "01:23:45")
        self.assertEqual(format_seconds_to_hms(930), "15:30")
        self.assertEqual(format_seconds_to_hms(45), "00:45")
        with self.assertRaises(ValueError):
            format_seconds_to_hms(-5)

    def test_extract_endpoint_validation(self):
        # Missing streamer_id and url
        r1 = client.post("/api/clip/extract", json={"start_time": "00:01:00", "end_time": "00:02:00"})
        self.assertEqual(r1.status_code, 400)

        # Unapproved external domain
        r2 = client.post("/api/clip/extract", json={"url": "https://evil.com/live", "start_time": "00:01:00", "end_time": "00:02:00"})
        self.assertEqual(r2.status_code, 400)

        # Option injection attempt
        r3 = client.post("/api/clip/extract", json={"url": "--help", "start_time": "00:01:00", "end_time": "00:02:00"})
        self.assertEqual(r3.status_code, 400)

        # Inverted timestamps
        r4 = client.post("/api/clip/extract", json={"url": "https://www.youtube.com/watch?v=123", "start_time": "00:05:00", "end_time": "00:02:00"})
        self.assertEqual(r4.status_code, 400)

        # Exceeds max window (default 1800s)
        r5 = client.post("/api/clip/extract", json={"url": "https://www.youtube.com/watch?v=123", "start_time": "00:00:00", "end_time": "01:00:00"})
        self.assertEqual(r5.status_code, 400)

    def test_trim_endpoint_security_and_validation(self):
        # Path traversal rejection
        r1 = client.post("/api/clip/trim", json={"filename": "../../etc/passwd.mp4", "start_sec": 1.0, "end_sec": 3.0})
        self.assertEqual(r1.status_code, 400)

        # Non-existent clip
        r2 = client.post("/api/clip/trim", json={"filename": "non_existent_clip_12345.mp4", "start_sec": 1.0, "end_sec": 3.0})
        self.assertEqual(r2.status_code, 404)

        # Inverted time range
        r3 = client.post("/api/clip/trim", json={"filename": "test.mp4", "start_sec": 5.0, "end_sec": 2.0})
        self.assertEqual(r3.status_code, 400)

    def test_trim_endpoint_execution(self):
        CLIPS_DIR.mkdir(parents=True, exist_ok=True)
        test_source = CLIPS_DIR / "unit_test_source.mp4"
        # Generate short 4-second dummy test video with ffmpeg
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=4:size=320x240:rate=25",
            "-f", "lavfi", "-i", "sine=frequency=1000:duration=4",
            "-c:v", "libx264", "-c:a", "aac",
            str(test_source)
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"FFmpeg dummy video creation failed: {proc.stderr}")
        self.assertTrue(test_source.exists())

        try:
            # Test fast mode (-c copy)
            r_fast = client.post("/api/clip/trim", json={
                "filename": "unit_test_source.mp4",
                "start_sec": 1.0,
                "end_sec": 3.0,
                "mode": "fast",
                "title_suffix": "unit_fast"
            })
            self.assertEqual(r_fast.status_code, 200)
            data_fast = r_fast.json()
            self.assertEqual(data_fast["status"], "ok")
            fast_clip_path = CLIPS_DIR / data_fast["clip"]["filename"]
            self.assertTrue(fast_clip_path.exists())
            self.assertGreater(fast_clip_path.stat().st_size, 0)
            fast_clip_path.unlink(missing_ok=True)

            # Test exact mode (re-encode)
            r_exact = client.post("/api/clip/trim", json={
                "filename": "unit_test_source.mp4",
                "start_sec": 0.5,
                "end_sec": 2.5,
                "mode": "exact",
                "title_suffix": "unit_exact"
            })
            self.assertEqual(r_exact.status_code, 200)
            data_exact = r_exact.json()
            self.assertEqual(data_exact["status"], "ok")
            exact_clip_path = CLIPS_DIR / data_exact["clip"]["filename"]
            self.assertTrue(exact_clip_path.exists())
            self.assertGreater(exact_clip_path.stat().st_size, 0)
            exact_clip_path.unlink(missing_ok=True)

        finally:
            test_source.unlink(missing_ok=True)

if __name__ == "__main__":
    unittest.main()
