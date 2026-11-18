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
    analyse_chat_spikes_openrouter,
    analyse_transcript_openrouter,
    call_openrouter_chat,
    get_cookie_args,
    parse_live_chat_excitement,
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

    def test_parse_live_chat_excitement(self):
        """Live chat JSON must be parsed and excitement velocity calculated."""
        tmp_chat = Path("/tmp/test_sample_live_chat.json")
        sample_records = [
            # Low activity window at 10s (offset 10000 ms)
            {
                "replayChatItemAction": {
                    "videoOffsetTimeMsec": "10000",
                    "actions": [{
                        "addChatItemAction": {
                            "item": {
                                "liveChatTextMessageRenderer": {
                                    "authorName": {"simpleText": "user1"},
                                    "message": {"runs": [{"text": "hello everyone"}]}
                                }
                            }
                        }
                    }]
                }
            },
            # High activity hype cluster at 120s (offset 120000 ms)
            {
                "replayChatItemAction": {
                    "videoOffsetTimeMsec": "120000",
                    "actions": [{
                        "addChatItemAction": {
                            "item": {
                                "liveChatTextMessageRenderer": {
                                    "authorName": {"simpleText": "user2"},
                                    "message": {"runs": [{"text": "OMG HE CRASHED THE TANK LMAOOO 💀"}]}
                                }
                            }
                        }
                    }]
                }
            },
            {
                "replayChatItemAction": {
                    "videoOffsetTimeMsec": "122000",
                    "actions": [{
                        "addChatItemAction": {
                            "item": {
                                "liveChatTextMessageRenderer": {
                                    "authorName": {"simpleText": "user3"},
                                    "message": {"runs": [{"text": "WTF NO WAYYY POG"}]}
                                }
                            }
                        }
                    }]
                }
            },
            {
                "replayChatItemAction": {
                    "videoOffsetTimeMsec": "125000",
                    "actions": [{
                        "addChatItemAction": {
                            "item": {
                                "liveChatTextMessageRenderer": {
                                    "authorName": {"simpleText": "user4"},
                                    "message": {"runs": [{"text": "CLIP THAT RIGHT NOW 🔥🔥"}]}
                                }
                            }
                        }
                    }]
                }
            },
        ]

        with open(tmp_chat, "w", encoding="utf-8") as f:
            for rec in sample_records:
                f.write(json.dumps(rec) + "\n")

        try:
            peaks = parse_live_chat_excitement(tmp_chat, window_sec=35, top_k=5)
            self.assertGreaterEqual(len(peaks), 1)
            top_peak = peaks[0]
            # The cluster at 120s should be detected
            self.assertEqual(top_peak["start_sec"], 105)  # bucket index 120 // 35 = 3 -> 3 * 35 = 105
            self.assertEqual(top_peak["end_sec"], 140)
            self.assertGreater(top_peak["raw_score"], 5)
            self.assertGreaterEqual(top_peak["msgs"], 3)
            self.assertGreaterEqual(top_peak["hype"], 3)
        finally:
            tmp_chat.unlink(missing_ok=True)

    @patch("gta6_clipper.openrouter.call_openrouter_chat")
    def test_analyse_chat_spikes_openrouter_success(self, mock_chat):
        """Mocked OpenRouter response from chat spikes must yield structured moments with source='live_chat'."""
        mock_response = json.dumps({
            "moments": [
                {
                    "start": "00:02:00",
                    "end": "00:02:35",
                    "score": 9,
                    "title": "Tank Stunt Explosion",
                    "reason": "Viewers erupted in chat as tank exploded mid-air"
                }
            ]
        })
        mock_chat.return_value = mock_response

        peaks = [{
            "start": "02:00",
            "end": "02:35",
            "start_sec": 120.0,
            "end_sec": 155.0,
            "duration_sec": 35.0,
            "raw_score": 15.0,
            "msgs": 25,
            "hype": 10,
            "samples": ["@user: OMG HE CRASHED", "@user2: LMAOO RIP"],
        }]

        moments = analyse_chat_spikes_openrouter("fake-key", peaks)
        self.assertEqual(len(moments), 1)
        m = moments[0]
        self.assertEqual(m["title"], "Tank Stunt Explosion")
        self.assertEqual(m["score"], 9)
        self.assertEqual(m["source"], "live_chat")
        self.assertEqual(m["start_sec"], 120.0)

    def test_cookie_args_detection(self):
        """get_cookie_args should return extractor args by default and cookies when configured."""
        args_default = get_cookie_args()
        self.assertIn("--extractor-args", args_default)
        self.assertIn("youtube:player_client=android,web", args_default)

        # Test with YOUTUBE_COOKIES
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            with patch.dict("os.environ", {"YOUTUBE_COOKIES": "# Netscape HTTP Cookie File\n.youtube.com TRUE / FALSE 1700000000 SID sample"}):
                cookie_args = get_cookie_args(Path(td))
                self.assertIn("--cookies", cookie_args)


if __name__ == "__main__":
    unittest.main()
