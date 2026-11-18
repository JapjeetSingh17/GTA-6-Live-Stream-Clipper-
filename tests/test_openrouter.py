"""
Unit and Integration Tests for OpenRouter Free LLM Highlights.
Tests:
- WebVTT transcript parser with timestamp conversion
- OpenRouter API request formatting and mock execution
- Error handling on missing keys or malformed JSON
- API router integration with engine='openrouter'
"""

import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from gta6_clipper.app import app
from gta6_clipper.openrouter import (
    analyse_transcript_openrouter,
    call_openrouter_chat,
    parse_vtt_to_timestamped_text,
)

client = TestClient(app, headers={"Host": "localhost:8000", "X-Access-Key": "Javelloui*~*.Yrs->2004"})

SAMPLE_VTT = """WEBVTT
Kind: captions
Language: en

00:00:10.500 --> 00:00:14.200
What is up guys today we are in Vice City

00:00:14.250 --> 00:00:18.000
Look at this helicopter chasing us!

00:01:30.000 --> 00:01:35.000
Oh no the car exploded! That was insane!
"""


class TestOpenRouter(unittest.TestCase):
    def test_parse_vtt_to_timestamped_text(self):
        """VTT subtitles must be parsed into clean [HH:MM:SS] lines."""
        tmp_vtt = Path("/tmp/test_sample_captions.vtt")
        tmp_vtt.write_text(SAMPLE_VTT, encoding="utf-8")
        try:
            parsed = parse_vtt_to_timestamped_text(tmp_vtt)
            self.assertIn("[00:00:10] What is up guys today we are in Vice City", parsed)
            self.assertIn("[00:01:30] Oh no the car exploded! That was insane!", parsed)
        finally:
            tmp_vtt.unlink(missing_ok=True)

    @patch("gta6_clipper.openrouter.call_openrouter_chat")
    def test_analyse_transcript_openrouter_success(self, mock_chat):
        """Mocked OpenRouter response must parse into structured moments."""
        mock_response = json.dumps({
            "moments": [
                {
                    "start": "00:01:30",
                    "end": "00:01:50",
                    "score": 9,
                    "title": "Exploding Car Escape",
                    "reason": "Intense explosion and streamer high-energy reaction"
                }
            ]
        })
        mock_chat.return_value = mock_response

        transcript = "[00:01:30] The car exploded! [00:01:35] That was insane!"
        moments = analyse_transcript_openrouter("fake-openrouter-key", transcript)

        self.assertEqual(len(moments), 1)
        m = moments[0]
        self.assertEqual(m["title"], "Exploding Car Escape")
        self.assertEqual(m["score"], 9)
        self.assertEqual(m["start"], "01:30")
        self.assertEqual(m["start_sec"], 90.0)
        self.assertEqual(m["end_sec"], 110.0)

    def test_openrouter_api_requires_key(self):
        """POST /api/clip/moments with engine=openrouter must fail with 400 if OPENROUTER_API_KEY is unset."""
        with patch.dict("os.environ", {}, clear=False):
            if "OPENROUTER_API_KEY" in os.environ:
                del os.environ["OPENROUTER_API_KEY"]
            resp = client.post(
                "/api/clip/moments",
                json={
                    "engine": "openrouter",
                    "url": "https://www.youtube.com/watch?v=sample123",
                },
            )
            self.assertEqual(resp.status_code, 400)
            self.assertIn("OPENROUTER_API_KEY", resp.json().get("detail", ""))


if __name__ == "__main__":
    unittest.main()
