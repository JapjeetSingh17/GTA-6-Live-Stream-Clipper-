"""
Test Harness and Common Utilities for GTA6-ZeroHour E2E Tests.
Provides opaque-box HTTP communication, server lifecycle management,
synthetic stream segment creation, and media inspection.
"""

import atexit
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple, Union

# Robust path resolution accommodating directory trailing spaces
REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = REPO_ROOT / "gta6_clipper"
STREAMS_DIR = APP_DIR / "streams"
CLIPS_DIR = APP_DIR / "clips"
LOGS_DIR = APP_DIR / "logs"
STATIC_DIR = APP_DIR / "static"
STREAMERS_JSON = APP_DIR / "streamers.json"
MARKS_JSON = APP_DIR / "marks.json"
VENV_PYTHON = APP_DIR / ".venv" / "bin" / "python"

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8000
BASE_URL = os.environ.get("GTA6_TEST_BASE_URL", f"http://{SERVER_HOST}:{SERVER_PORT}")

_SERVER_PROCESS: Optional[subprocess.Popen] = None


class HTTPResponseWrapper:
    """Encapsulates HTTP response details for test assertions."""

    def __init__(self, status: int, headers: Dict[str, str], body: bytes):
        self.status = status
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.body = body

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        try:
            return json.loads(self.text)
        except Exception as exc:
            raise ValueError(
                f"Failed to decode response as JSON: {self.text[:300]}"
            ) from exc

    def get_header(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name.lower(), default)


def is_port_open(host: str = SERVER_HOST, port: int = SERVER_PORT) -> bool:
    """Checks if a TCP port is open and listening."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        return sock.connect_ex((host, port)) == 0
    finally:
        sock.close()


def is_server_responding() -> bool:
    """Sends a quick HTTP probe to /api/status."""
    if not is_port_open():
        return False
    try:
        resp = raw_http_request("GET", "/api/status", host="127.0.0.1:8000", timeout=2.0)
        return resp.status == 200
    except Exception:
        return False


def start_test_server(timeout_sec: float = 8.0) -> bool:
    """Starts the GTA6-ZeroHour server in a background process if not already running."""
    global _SERVER_PROCESS
    if is_server_responding():
        return True

    app_py = APP_DIR / "app.py"
    if not app_py.exists():
        return False

    py_bin = str(VENV_PYTHON) if VENV_PYTHON.exists() else "python3"
    cmd = [
        py_bin,
        "-m",
        "uvicorn",
        "app:app",
        "--host",
        SERVER_HOST,
        "--port",
        str(SERVER_PORT),
    ]

    _SERVER_PROCESS = subprocess.Popen(
        cmd,
        cwd=str(APP_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )

    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if is_server_responding():
            return True
        time.sleep(0.3)

    return False


def stop_test_server():
    """Cleanly stops the background test server process if started by harness."""
    global _SERVER_PROCESS
    if _SERVER_PROCESS is not None:
        try:
            os.killpg(os.getpgid(_SERVER_PROCESS.pid), signal.SIGTERM)
            _SERVER_PROCESS.wait(timeout=3)
        except Exception:
            try:
                os.killpg(os.getpgid(_SERVER_PROCESS.pid), signal.SIGKILL)
            except Exception:
                pass
        _SERVER_PROCESS = None


atexit.register(stop_test_server)


def raw_http_request(
    method: str,
    path: str,
    headers: Optional[Dict[str, str]] = None,
    body: Optional[Union[str, bytes, dict]] = None,
    host: Optional[str] = "127.0.0.1:8000",
    timeout: float = 5.0,
) -> HTTPResponseWrapper:
    """Low-level HTTP request using http.client to allow arbitrary Host and Origin headers."""
    conn = http.client.HTTPConnection(SERVER_HOST, SERVER_PORT, timeout=timeout)
    req_headers = {}
    if host is not None:
        req_headers["Host"] = host

    payload: Optional[bytes] = None
    if isinstance(body, dict):
        payload = json.dumps(body).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    elif isinstance(body, str):
        payload = body.encode("utf-8")
    elif isinstance(body, bytes):
        payload = body

    if headers:
        for k, v in headers.items():
            req_headers[k] = v

    try:
        conn.request(method, path, body=payload, headers=req_headers)
        resp = conn.getresponse()
        resp_body = resp.read()
        resp_headers = dict(resp.getheaders())
        return HTTPResponseWrapper(status=resp.status, headers=resp_headers, body=resp_body)
    finally:
        conn.close()


def create_synthetic_segment(
    streamer_id: str,
    timestamp_str: str,
    duration_sec: int = 6,
    resolution: str = "640x360",
) -> Path:
    """Generates an authentic 6-second MPEG-TS H.264/AAC segment using ffmpeg."""
    target_dir = STREAMS_DIR / streamer_id
    target_dir.mkdir(parents=True, exist_ok=True)
    out_file = target_dir / f"{timestamp_str}.ts"

    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=duration={duration_sec}:size={resolution}:rate=30",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=1000:duration={duration_sec}",
        "-c:v",
        "libx264",
        "-g",
        "60",
        "-c:a",
        "aac",
        "-f",
        "mpegts",
        str(out_file),
    ]

    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"FFmpeg failed to create test segment: {res.stderr}")

    return out_file


def probe_video(video_path: Union[str, Path]) -> Dict[str, Any]:
    """Inspects a video file using ffprobe and extracts container & codec metadata."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration,format_name,size",
        "-show_entries",
        "stream=codec_name,codec_type,width,height,duration",
        "-print_format",
        "json",
        str(video_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {video_path}: {res.stderr}")

    return json.loads(res.stdout)


def clean_test_artifacts(
    streamer_ids: Optional[List[str]] = None, clip_prefixes: Optional[List[str]] = None
):
    """Cleans up synthetic segments, test clips, and test marks."""
    if streamer_ids:
        for sid in streamer_ids:
            s_dir = STREAMS_DIR / sid
            if s_dir.exists():
                for p in s_dir.glob("*.ts"):
                    p.unlink(missing_ok=True)
                try:
                    s_dir.rmdir()
                except OSError:
                    pass

    if clip_prefixes:
        for p in CLIPS_DIR.glob("*.mp4"):
            for prefix in clip_prefixes:
                if p.name.startswith(prefix):
                    p.unlink(missing_ok=True)
