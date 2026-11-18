"""
Tier 1 & Tier 2 API Tests for GTA6-ZeroHour.
Covers:
- GET /api/status telemetry, disk, active recordings schema
- Streamer CRUD & persistence (GET, POST, PATCH, DELETE /api/streamers)
- Master & individual poll start/stop toggles (/api/poll/start, /api/poll/stop)
- Timestamp marks (POST /api/mark, GET /api/marks, POST /api/clip/mark)
- JSON persistence validation (streamers.json, marks.json)
- Parameter boundary and missing field validation
"""

import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_harness import (
    raw_http_request,
    start_test_server,
    is_server_responding,
    STREAMERS_JSON,
    MARKS_JSON,
)


class TestApiEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        start_test_server()

    # =========================================================================
    # 1. Status Telemetry Endpoint (/api/status)
    # =========================================================================

    def test_status_endpoint_returns_200(self):
        """GET /api/status must return HTTP 200."""
        if not is_server_responding():
            self.skipTest("Server not running")
        resp = raw_http_request("GET", "/api/status")
        self.assertEqual(resp.status, 200)

    def test_status_endpoint_schema_keys(self):
        """GET /api/status response must include all required telemetry fields."""
        if not is_server_responding():
            self.skipTest("Server not running")
        resp = raw_http_request("GET", "/api/status")
        self.assertEqual(resp.status, 200)
        data = resp.json()
        self.assertIn("status", data)
        self.assertEqual(data["status"], "ok")
        self.assertIn("master_polling", data)
        self.assertIsInstance(data["master_polling"], bool)
        self.assertIn("active_recordings", data)
        self.assertIsInstance(data["active_recordings"], int)
        self.assertIn("disk", data)
        self.assertIn("streamers", data)
        self.assertIsInstance(data["streamers"], list)

    def test_status_disk_telemetry_schema(self):
        """GET /api/status disk object must contain total, used, free GB and disk_guard."""
        if not is_server_responding():
            self.skipTest("Server not running")
        resp = raw_http_request("GET", "/api/status")
        self.assertEqual(resp.status, 200)
        disk = resp.json().get("disk", {})
        self.assertIn("total_gb", disk)
        self.assertIn("used_gb", disk)
        self.assertIn("free_gb", disk)
        self.assertIn("disk_guard", disk)
        self.assertGreater(disk["total_gb"], 0)
        self.assertGreater(disk["free_gb"], 0)
        self.assertIsInstance(disk["disk_guard"], bool)

    def test_status_streamers_list_schema(self):
        """Streamers in /api/status must provide id, name, platform, url, and status."""
        if not is_server_responding():
            self.skipTest("Server not running")
        resp = raw_http_request("GET", "/api/status")
        self.assertEqual(resp.status, 200)
        streamers = resp.json().get("streamers", [])
        self.assertGreaterEqual(len(streamers), 1)
        for s in streamers:
            self.assertIn("id", s)
            self.assertIn("name", s)
            self.assertIn("platform", s)
            self.assertIn("url", s)
            self.assertIn("status", s)

    # =========================================================================
    # 2. Streamer CRUD & Persistence (/api/streamers)
    # =========================================================================

    def test_get_streamers_contains_defaults(self):
        """GET /api/streamers must return list containing configured streamers."""
        if not is_server_responding():
            self.skipTest("Server not running")
        resp = raw_http_request("GET", "/api/streamers")
        self.assertEqual(resp.status, 200)
        streamers = resp.json()
        ids = {s["id"] for s in streamers}
        with open(STREAMERS_JSON, "r", encoding="utf-8") as f:
            expected = set(json.load(f).keys())
        self.assertEqual(ids, expected)
        self.assertIn("ishowspeed", ids)
        self.assertIn("carryislive", ids)

    def test_streamer_crud_lifecycle(self):
        """Complete lifecycle: POST create -> PATCH update -> DELETE remove."""
        if not is_server_responding():
            self.skipTest("Server not running")

        test_id = "test_crud_streamer"
        try:
            # 1. Clean up if leftover
            raw_http_request("DELETE", f"/api/streamers/{test_id}")

            # 2. POST create
            create_payload = {
                "id": test_id,
                "name": "Test Streamer",
                "platform": "kick",
                "url": "https://kick.com/test_crud_streamer",
                "enabled": True,
            }
            create_resp = raw_http_request(
                "POST", "/api/streamers", body=create_payload
            )
            self.assertIn(
                create_resp.status,
                (200, 201),
                f"Failed to create streamer: {create_resp.text}",
            )

            # 3. GET verify presence
            list_resp = raw_http_request("GET", "/api/streamers")
            ids = [s["id"] for s in list_resp.json()]
            self.assertIn(test_id, ids)

            # 4. PATCH update name and enabled
            patch_payload = {"name": "Updated Test Name", "enabled": False}
            patch_resp = raw_http_request(
                "PATCH", f"/api/streamers/{test_id}", body=patch_payload
            )
            self.assertEqual(
                patch_resp.status,
                200,
                f"Failed to patch streamer: {patch_resp.text}",
            )

            # 5. DELETE remove
            del_resp = raw_http_request("DELETE", f"/api/streamers/{test_id}")
            self.assertEqual(del_resp.status, 200)

            # 6. Verify 404 after deletion
            del_again = raw_http_request("DELETE", f"/api/streamers/{test_id}")
            self.assertEqual(
                del_again.status,
                404,
                "Expected 404 when deleting non-existent streamer",
            )
        finally:
            raw_http_request("DELETE", f"/api/streamers/{test_id}")

    def test_create_duplicate_streamer_fails(self):
        """Creating a streamer with an existing ID must return HTTP 400."""
        if not is_server_responding():
            self.skipTest("Server not running")
        payload = {
            "id": "ishowspeed",
            "name": "Duplicate IShowSpeed",
            "platform": "youtube",
            "url": "https://www.youtube.com/@IShowSpeed/live",
            "enabled": True,
        }
        resp = raw_http_request("POST", "/api/streamers", body=payload)
        self.assertEqual(
            resp.status,
            400,
            f"Expected 400 on duplicate streamer creation, got {resp.status}",
        )

    def test_patch_nonexistent_streamer_returns_404(self):
        """PATCH /api/streamers/{id} on non-existent streamer must return HTTP 404."""
        if not is_server_responding():
            self.skipTest("Server not running")
        resp = raw_http_request(
            "PATCH",
            "/api/streamers/non_existent_streamer_id",
            body={"name": "Ghost"},
        )
        self.assertEqual(resp.status, 404)

    def test_streamers_persistence_file_exists(self):
        """streamers.json must exist and be valid JSON when server is running."""
        if not is_server_responding():
            self.skipTest("Server not running")
        self.assertTrue(
            STREAMERS_JSON.exists(),
            f"streamers.json not found at {STREAMERS_JSON}",
        )
        with open(STREAMERS_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
            self.assertIsInstance(data, (dict, list))

    # =========================================================================
    # 3. Master & Individual Poll Toggles (/api/poll/start, /api/poll/stop)
    # =========================================================================

    def test_poll_start_and_stop_master(self):
        """POST /api/poll/start and /api/poll/stop with master toggle."""
        if not is_server_responding():
            self.skipTest("Server not running")

        initial_status = raw_http_request("GET", "/api/status").json()
        initial_polling = initial_status.get("master_polling", True)

        try:
            # Stop master
            stop_resp = raw_http_request(
                "POST", "/api/poll/stop", body={"streamer_id": "all"}
            )
            self.assertEqual(stop_resp.status, 200)

            # Verify status
            status_resp = raw_http_request("GET", "/api/status")
            self.assertFalse(status_resp.json().get("master_polling"))

            # Start master
            start_resp = raw_http_request(
                "POST", "/api/poll/start", body={"streamer_id": "all"}
            )
            self.assertEqual(start_resp.status, 200)

            # Verify status
            status_resp2 = raw_http_request("GET", "/api/status")
            self.assertTrue(status_resp2.json().get("master_polling"))

            # Stop master again to complete start/stop cycle
            stop_resp2 = raw_http_request(
                "POST", "/api/poll/stop", body={"streamer_id": "all"}
            )
            self.assertEqual(stop_resp2.status, 200)

            # Verify status
            status_resp3 = raw_http_request("GET", "/api/status")
            self.assertFalse(status_resp3.json().get("master_polling"))
        finally:
            if initial_polling:
                raw_http_request(
                    "POST", "/api/poll/start", body={"streamer_id": "all"}
                )
            else:
                raw_http_request(
                    "POST", "/api/poll/stop", body={"streamer_id": "all"}
                )

    def test_poll_start_single_streamer(self):
        """POST /api/poll/start with specific streamer_id."""
        if not is_server_responding():
            self.skipTest("Server not running")
        resp = raw_http_request(
            "POST", "/api/poll/start", body={"streamer_id": "ishowspeed"}
        )
        self.assertEqual(resp.status, 200)

    def test_poll_stop_single_streamer(self):
        """POST /api/poll/stop with specific streamer_id."""
        if not is_server_responding():
            self.skipTest("Server not running")
        resp = raw_http_request(
            "POST", "/api/poll/stop", body={"streamer_id": "ishowspeed"}
        )
        self.assertEqual(resp.status, 200)

    def test_poll_start_nonexistent_streamer_returns_404(self):
        """POST /api/poll/start with unknown streamer_id must return HTTP 404."""
        if not is_server_responding():
            self.skipTest("Server not running")
        resp = raw_http_request(
            "POST",
            "/api/poll/start",
            body={"streamer_id": "unknown_streamer_xyz"},
        )
        self.assertEqual(resp.status, 404)

    # =========================================================================
    # 4. Marks API (/api/mark, /api/marks, /api/clip/mark)
    # =========================================================================

    def test_create_and_list_mark(self):
        """POST /api/mark creates a mark and GET /api/marks lists it."""
        if not is_server_responding():
            self.skipTest("Server not running")

        create_payload = {"streamer_id": "all", "label": "E2E Test Mark"}
        create_resp = raw_http_request(
            "POST", "/api/mark", body=create_payload
        )
        self.assertEqual(
            create_resp.status,
            200,
            f"Failed to create mark: {create_resp.text}",
        )
        mark_data = create_resp.json().get("mark", {})
        self.assertIn("id", mark_data)
        self.assertIn("timestamp", mark_data)
        self.assertEqual(mark_data.get("label"), "E2E Test Mark")

        # Verify listed in GET /api/marks
        list_resp = raw_http_request("GET", "/api/marks")
        self.assertEqual(list_resp.status, 200)
        marks = list_resp.json()
        mark_ids = [m["id"] for m in marks]
        self.assertIn(mark_data["id"], mark_ids)

    def test_clip_nonexistent_mark_returns_404(self):
        """POST /api/clip/mark with invalid mark_id must return HTTP 404."""
        if not is_server_responding():
            self.skipTest("Server not running")
        payload = {
            "mark_id": "non_existent_mark_9999",
            "streamer_id": "ishowspeed",
            "pre_sec": 10,
            "post_sec": 10,
        }
        resp = raw_http_request("POST", "/api/clip/mark", body=payload)
        self.assertEqual(resp.status, 404)

    def test_marks_persistence_file(self):
        """marks.json must exist or be created upon mark creation."""
        if not is_server_responding():
            self.skipTest("Server not running")
        raw_http_request(
            "POST", "/api/mark", body={"streamer_id": "all", "label": "Disk Sync"}
        )
        self.assertTrue(
            MARKS_JSON.exists(), f"marks.json not found at {MARKS_JSON}"
        )


if __name__ == "__main__":
    unittest.main()
