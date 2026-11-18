#!/usr/bin/env python3
"""Unified E2E Test Suite Runner for GTA6-ZeroHour.

Executes all 4 tiers of tests across all feature modules and outputs a
structured test execution report.
"""

from datetime import datetime
import os
from pathlib import Path
import sys
import time
import unittest

# Ensure tests/ directory is on sys.path
TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))

from test_harness import (
    start_test_server,
    stop_test_server,
    is_server_responding,
    raw_http_request,
    STREAMS_DIR,
    CLIPS_DIR,
    MARKS_JSON,
)


def clean_test_artifacts(
    streamer_ids: list[str] | None = None,
    clip_prefixes: list[str] | None = None,
    restore_master_polling: bool = True,
) -> None:
    """Cleans up test artifacts: stops polling, removes all .ts segments, resets marks.json, and restores user polling state."""
    # 1. Stop active master or streamer polling via POST /api/poll/stop {"streamer_id": "all"}
    if is_server_responding():
        try:
            raw_http_request(
                "POST", "/api/poll/stop", body={"streamer_id": "all"}
            )
        except Exception as exc:
            print(f"[!] Notice during cleanup: Failed to stop polling: {exc}")

    # 2. Recursively remove any .ts segment files under gta6_clipper/streams/ (including xqc/)
    if STREAMS_DIR.exists():
        for ts_path in STREAMS_DIR.rglob("*.ts"):
            try:
                ts_path.unlink(missing_ok=True)
            except Exception:
                pass

        # Also clean up empty streamer test directories if requested
        synthetic_dirs = {"test_clipper_streamer", "scenario_test_streamer", "scenario_crud_lifecycle", "test_crud_streamer", "xqc"}
        if streamer_ids:
            synthetic_dirs.update(streamer_ids)
        for sid in synthetic_dirs:
            s_dir = STREAMS_DIR / sid
            if s_dir.exists():
                try:
                    s_dir.rmdir()
                except OSError:
                    pass

    # 3. Clean up test clips
    if clip_prefixes and CLIPS_DIR.exists():
        for p in CLIPS_DIR.glob("*.mp4"):
            for prefix in clip_prefixes:
                if p.name.startswith(prefix):
                    try:
                        p.unlink(missing_ok=True)
                    except Exception:
                        pass

    # 4. Reset gta6_clipper/marks.json to []
    try:
        MARKS_JSON.parent.mkdir(parents=True, exist_ok=True)
        MARKS_JSON.write_text("[]", encoding="utf-8")
    except Exception as exc:
        print(f"[!] Notice during cleanup: Failed to reset marks.json: {exc}")

    # 5. Restore master polling if requested
    if restore_master_polling and is_server_responding():
        try:
            raw_http_request(
                "POST", "/api/poll/start", body={"streamer_id": "all"}
            )
        except Exception as exc:
            print(f"[!] Notice during cleanup: Failed to restore master polling: {exc}")

import test_security
import test_api
import test_clipper
import test_recording_resilience
import test_static_ui
import test_scenarios
import test_extractor
import test_moments
import test_auth


def create_full_suite() -> unittest.TestSuite:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    suite.addTests(loader.loadTestsFromModule(test_security))
    suite.addTests(loader.loadTestsFromModule(test_api))
    suite.addTests(loader.loadTestsFromModule(test_clipper))
    suite.addTests(loader.loadTestsFromModule(test_recording_resilience))
    suite.addTests(loader.loadTestsFromModule(test_static_ui))
    suite.addTests(loader.loadTestsFromModule(test_scenarios))
    suite.addTests(loader.loadTestsFromModule(test_extractor))
    suite.addTests(loader.loadTestsFromModule(test_moments))
    suite.addTests(loader.loadTestsFromModule(test_auth))

    return suite


def main():
    print("=" * 72)
    print("  GTA6-ZeroHour E2E Test Suite Orchestrator")
    print(f"  Execution Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Working Directory: {REPO_ROOT}")
    print("=" * 72)

    # Check or launch server
    server_started_by_runner = False
    if is_server_responding():
        print("[+] Live server already detected at http://127.0.0.1:8000")
    else:
        print("[*] Launching GTA6-ZeroHour test server on 127.0.0.1:8000...")
        if start_test_server(timeout_sec=6.0):
            server_started_by_runner = True
            print("[+] Test server successfully started and responding.")
        else:
            print(
                "[!] Notice: Test server could not be started (app.py may still be under development)."
            )
            print(
                "[*] Running standalone and requirement-specification tests..."
            )

    # Pre-test cleanup of leftover synthetic test artifacts
    clean_test_artifacts(
        streamer_ids=["test_clipper_streamer", "scenario_test_streamer", "scenario_crud_lifecycle", "test_crud_streamer"],
        clip_prefixes=["test_clipper_streamer", "scenario_test_streamer", "scenario_crud_lifecycle", "test_crud_streamer"],
        restore_master_polling=False,
    )

    # Ensure temporary scenario streamer exists during test execution for scenario validation
    if is_server_responding():
        try:
            raw_http_request(
                "POST",
                "/api/streamers",
                body={
                    "id": "scenario_test_streamer",
                    "name": "Scenario Streamer",
                    "platform": "youtube",
                    "url": "https://www.youtube.com/@ScenarioStreamer/live",
                    "enabled": True,
                },
            )
        except Exception:
            pass

    suite = create_full_suite()
    total_tests = suite.countTestCases()
    print(f"[*] Loaded {total_tests} test cases across 6 test modules.")
    print("-" * 72)

    runner = unittest.TextTestRunner(verbosity=2)
    start_time = time.time()
    result = None
    try:
        result = runner.run(suite)
    finally:
        elapsed = time.time() - start_time
        # Clean up temporary test streamer from streamers.json
        if is_server_responding():
            try:
                raw_http_request("DELETE", "/api/streamers/scenario_test_streamer")
            except Exception:
                pass

        # Clean up test artifacts (stops polling, removes .ts segments, resets marks.json, restores master polling)
        clean_test_artifacts(
            streamer_ids=["test_clipper_streamer", "scenario_test_streamer", "scenario_crud_lifecycle", "test_crud_streamer"],
            clip_prefixes=["test_clipper_streamer", "scenario_test_streamer", "scenario_crud_lifecycle", "test_crud_streamer"],
            restore_master_polling=True,
        )

        if server_started_by_runner:
            print("[*] Stopping background test server...")
            stop_test_server()

    print("-" * 72)
    print("  TEST EXECUTION SUMMARY")
    print("-" * 72)
    print(f"  Total Tests Run: {result.testsRun}")
    print(f"  Passed:          {result.testsRun - len(result.failures) - len(result.errors)}")
    print(f"  Failures:        {len(result.failures)}")
    print(f"  Errors:          {len(result.errors)}")
    print(f"  Skipped:         {len(result.skipped)}")
    print(f"  Total Duration:  {elapsed:.3f}s")
    print("=" * 72)

    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
