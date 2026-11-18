"""
Live Smoke Verification Script for GTA6-ZeroHour
Tests real-world Twitch stream ingestion, zero-re-encode clipping,
mark clipping, and ffprobe stream inspection against live streamer:
https://www.twitch.tv/snowleopard_1527
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.error

BASE_URL = "http://127.0.0.1:8000"
STREAMER_ID = "snowleopard_1527"
STREAM_URL = "https://www.twitch.tv/snowleopard_1527"

ROOT_DIR = Path(__file__).resolve().parent.parent
APP_DIR = ROOT_DIR / "gta6_clipper"
STREAMS_DIR = APP_DIR / "streams" / STREAMER_ID
CLIPS_DIR = APP_DIR / "clips"
MARKS_FILE = APP_DIR / "marks.json"


def api_call(method: str, endpoint: str, data: dict = None) -> tuple[int, dict]:
    url = f"{BASE_URL}{endpoint}"
    req = urllib.request.Request(url, method=method)
    req.add_header("Content-Type", "application/json")
    body = json.dumps(data).encode("utf-8") if data is not None else None
    try:
        with urllib.request.urlopen(req, data=body, timeout=15) as resp:
            content = resp.read().decode("utf-8")
            return resp.status, json.loads(content) if content else {}
    except urllib.error.HTTPError as e:
        content = e.read().decode("utf-8")
        try:
            parsed = json.loads(content)
        except Exception:
            parsed = {"error": content}
        return e.code, parsed


def inspect_media_file(file_path: Path) -> dict:
    cmd_v = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,width,height",
        "-of", "json", str(file_path)
    ]
    res_v = subprocess.run(cmd_v, capture_output=True, text=True, check=True)
    probe_v = json.loads(res_v.stdout)
    v_stream = probe_v.get("streams", [{}])[0]

    cmd_a = [
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=codec_name,sample_rate",
        "-of", "json", str(file_path)
    ]
    res_a = subprocess.run(cmd_a, capture_output=True, text=True, check=True)
    probe_a = json.loads(res_a.stdout)
    a_stream = probe_a.get("streams", [{}])[0]

    cmd_f = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration,size,format_name",
        "-of", "json", str(file_path)
    ]
    res_f = subprocess.run(cmd_f, capture_output=True, text=True, check=True)
    probe_f = json.loads(res_f.stdout)
    fmt = probe_f.get("format", {})

    return {
        "video_codec": v_stream.get("codec_name"),
        "resolution": f"{v_stream.get('width')}x{v_stream.get('height')}",
        "audio_codec": a_stream.get("codec_name"),
        "sample_rate": a_stream.get("sample_rate"),
        "duration": float(fmt.get("duration", 0.0)),
        "size_bytes": int(fmt.get("size", 0)),
        "format": fmt.get("format_name"),
    }


def main():
    print(f"=== Starting Live Smoke Verification with {STREAMER_ID} ===")
    print(f"Stream URL: {STREAM_URL}")

    # 1. Health check
    code, status_resp = api_call("GET", "/api/status")
    print(f"[1] Server status check: HTTP {code}, master_polling={status_resp.get('master_polling')}")
    assert code == 200, f"Server returned {code}"

    # 2. Add streamer
    streamer_payload = {
        "id": STREAMER_ID,
        "name": "Snow Leopard Live",
        "platform": "twitch",
        "url": STREAM_URL,
        "enabled": True,
    }
    code, add_resp = api_call("POST", "/api/streamers", streamer_payload)
    print(f"[2] Add streamer: HTTP {code}, response={add_resp}")
    assert code == 201, f"Failed to add streamer: {add_resp}"

    created_clips = []
    try:
        # 3. Start polling streamer
        code, poll_resp = api_call("POST", "/api/poll/start", {"streamer_id": STREAMER_ID})
        print(f"[3] Poll start: HTTP {code}, response={poll_resp}")
        assert code == 200, f"Failed to start poll: {poll_resp}"

        # 4. Wait for ~25-30 seconds of live recording into MPEG-TS segments
        print("[4] Recording live stream for ~28 seconds...")
        record_start = time.time()
        target_record_time = 28.0
        while time.time() - record_start < target_record_time:
            segs = list(STREAMS_DIR.glob("*.ts")) if STREAMS_DIR.exists() else []
            elapsed = time.time() - record_start
            print(f"    Elapsed: {elapsed:.1f}s | Active TS segments: {len(segs)}")
            time.sleep(4.0)

        segs = sorted(list(STREAMS_DIR.glob("*.ts"))) if STREAMS_DIR.exists() else []
        print(f"[4+] Recording phase completed. Total TS segments: {len(segs)}")
        for s in segs:
            print(f"    Segment: {s.name} ({s.stat().st_size} bytes)")
        assert len(segs) >= 2, f"Expected at least 2 segments, found {len(segs)}"

        # 5. Lookback clip (8 seconds)
        print("\n[5] Requesting 8-second lookback clip via POST /api/clip/lookback...")
        t0 = time.perf_counter()
        code, clip_resp = api_call("POST", "/api/clip/lookback", {"streamer_id": STREAMER_ID, "seconds": 8})
        cut_time_api = time.perf_counter() - t0
        print(f"    HTTP {code}, API roundtrip: {cut_time_api:.3f}s")
        assert code == 200, f"Lookback clip failed: {clip_resp}"
        clip_data = clip_resp.get("clip", {})
        clip_filename = clip_data.get("filename")
        server_cut_time = clip_data.get("cut_time_sec")
        print(f"    Clip generated: {clip_filename}")
        print(f"    Server cut time: {server_cut_time}s | Client roundtrip: {cut_time_api:.3f}s")
        clip_path = CLIPS_DIR / clip_filename
        assert clip_path.exists(), f"Clip file {clip_path} does not exist!"
        created_clips.append(clip_path)

        # 6. ffprobe inspection on lookback clip
        print("\n[6] Inspecting Lookback Clip via ffprobe...")
        media_info = inspect_media_file(clip_path)
        print(f"    Video Codec: {media_info['video_codec']} (must be h264)")
        print(f"    Audio Codec: {media_info['audio_codec']} (must be aac)")
        print(f"    Resolution:  {media_info['resolution']}")
        print(f"    Duration:    {media_info['duration']:.2f}s (target ~8s)")
        print(f"    File Size:   {media_info['size_bytes']} bytes")
        print(f"    Format:      {media_info['format']}")
        assert media_info["video_codec"] == "h264", f"Video codec is {media_info['video_codec']}, expected h264"
        assert media_info["audio_codec"] == "aac", f"Audio codec is {media_info['audio_codec']}, expected aac"
        assert media_info["duration"] > 0, "Duration must be > 0"

        # 7. Test MARK ALL and mark-based clipping
        print("\n[7] Testing MARK ALL and mark-based clipping...")
        code, mark_resp = api_call("POST", "/api/mark", {"streamer_id": "all", "label": "Live Smoke Mark"})
        print(f"    Mark creation: HTTP {code}, response={mark_resp}")
        assert code == 200, f"Mark creation failed: {mark_resp}"
        mark_id = mark_resp.get("mark", {}).get("id")

        time.sleep(2.0)  # brief pause

        t0_mark = time.perf_counter()
        code, mark_clip_resp = api_call(
            "POST", "/api/clip/mark",
            {"mark_id": mark_id, "streamer_id": STREAMER_ID, "pre_sec": 4, "post_sec": 4}
        )
        mark_cut_time_api = time.perf_counter() - t0_mark
        print(f"    Mark clip: HTTP {code}, roundtrip={mark_cut_time_api:.3f}s")
        assert code == 200, f"Mark clip failed: {mark_clip_resp}"
        mark_clip_data = mark_clip_resp.get("clip", {})
        mark_clip_filename = mark_clip_data.get("filename")
        print(f"    Mark clip filename: {mark_clip_filename}")
        print(f"    Server cut time: {mark_clip_data.get('cut_time_sec')}s")
        mark_clip_path = CLIPS_DIR / mark_clip_filename
        assert mark_clip_path.exists(), f"Mark clip file {mark_clip_path} does not exist"
        created_clips.append(mark_clip_path)

        # ffprobe inspection on mark clip
        print("    Inspecting Mark Clip via ffprobe...")
        mark_media_info = inspect_media_file(mark_clip_path)
        print(f"    Mark Clip Video Codec: {mark_media_info['video_codec']}")
        print(f"    Mark Clip Audio Codec: {mark_media_info['audio_codec']}")
        print(f"    Mark Clip Duration:    {mark_media_info['duration']:.2f}s")
        assert mark_media_info["video_codec"] == "h264"
        assert mark_media_info["audio_codec"] == "aac"
        assert mark_media_info["duration"] > 0

        print("\n[+] LIVE SMOKE VERIFICATION COMPLETED SUCCESSFULLY!")
        print(f"    Lookback cut time: {server_cut_time}s")
        print(f"    Mark clip cut time: {mark_clip_data.get('cut_time_sec')}s")
        print(f"    Stream resolution: {media_info['resolution']}")

    finally:
        # 8. Clean up
        print("\n[8] Cleaning up test resources...")
        # Stop polling
        code, stop_resp = api_call("POST", "/api/poll/stop", {"streamer_id": STREAMER_ID})
        print(f"    Poll stop: HTTP {code}")

        # Delete streamer
        code, del_resp = api_call("DELETE", f"/api/streamers/{STREAMER_ID}")
        print(f"    Delete streamer: HTTP {code}")

        # Wait a moment for ffmpeg/streamlink to terminate
        time.sleep(2.0)

        # Clean up segments in STREAMS_DIR
        if STREAMS_DIR.exists():
            for f in STREAMS_DIR.glob("*.ts"):
                try:
                    f.unlink()
                except OSError:
                    pass
            try:
                STREAMS_DIR.rmdir()
            except OSError:
                pass
            print(f"    Cleaned up stream directory: {STREAMS_DIR}")

        # Clean up test clips
        for c_path in created_clips:
            if c_path.exists():
                try:
                    c_path.unlink()
                    print(f"    Removed clip: {c_path.name}")
                except OSError:
                    pass

        # Clean up marks.json
        if MARKS_FILE.exists():
            try:
                with open(MARKS_FILE, "w", encoding="utf-8") as f:
                    json.dump([], f)
                print("    Reset marks.json to empty list.")
            except Exception as e:
                print(f"    Error resetting marks: {e}")

        # Also remove any leftover pid/log files for test streamer
        pid_file = APP_DIR / "logs" / f"{STREAMER_ID}.pid"
        if pid_file.exists():
            pid_file.unlink(missing_ok=True)
        log_file = APP_DIR / "logs" / f"{STREAMER_ID}.log"
        if log_file.exists():
            log_file.unlink(missing_ok=True)

        print("[+] Cleanup finished.")


if __name__ == "__main__":
    main()
