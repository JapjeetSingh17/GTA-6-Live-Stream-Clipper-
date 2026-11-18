import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import json
import logging
import os
from pathlib import Path
import random
import re
import shutil
import signal
import subprocess
import threading
import time
from typing import Optional
import urllib.parse
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("gta6_clipper")

# --- Paths & Directories ---
BASE_DIR = Path(__file__).resolve().parent
IS_SERVERLESS = os.getenv("VERCEL") == "1" or "AWS_LAMBDA_FUNCTION_NAME" in os.environ
DATA_DIR = Path("/tmp/gta6_clipper") if IS_SERVERLESS else BASE_DIR
STREAMS_DIR, CLIPS_DIR = DATA_DIR / "streams", DATA_DIR / "clips"
LOGS_DIR, STATIC_DIR = DATA_DIR / "logs", BASE_DIR / "static"
STREAMERS_FILE, MARKS_FILE = DATA_DIR / "streamers.json", DATA_DIR / "marks.json"

for d in (STREAMS_DIR, CLIPS_DIR, LOGS_DIR):
    try: d.mkdir(parents=True, exist_ok=True)
    except OSError: pass
if not IS_SERVERLESS:
    try: STATIC_DIR.mkdir(parents=True, exist_ok=True)
    except OSError: pass

# --- Security Controls & Whitelists ---
ALLOWED_HOSTS = {"127.0.0.1:8000", "localhost:8000", "127.0.0.1", "localhost", "testserver"}
ALLOWED_ORIGINS = {"http://127.0.0.1:8000", "http://localhost:8000"}
if IS_SERVERLESS or os.getenv("ALLOW_ALL_HOSTS", "").lower() in ("1", "true"):
    ALLOWED_HOSTS.add("*")
ALLOWED_DOMAINS = {"youtube.com", "youtu.be", "twitch.tv", "kick.com"}
STREAMER_ID_REGEX = re.compile(r"^[a-z0-9_-]{1,40}$")
TS_PATTERN = re.compile(r"^(\d{8}_\d{6})\.ts$")

DEFAULT_STREAMERS = {
    "ishowspeed": {"id": "ishowspeed", "name": "IShowSpeed", "platform": "youtube", "url": "https://www.youtube.com/@IShowSpeed/live", "enabled": True},
    "kaicenat": {"id": "kaicenat", "name": "Kai Cenat", "platform": "twitch", "url": "https://www.twitch.tv/kaicenat", "enabled": True},
    "xqc": {"id": "xqc", "name": "xQc", "platform": "kick", "url": "https://kick.com/xqc", "enabled": True},
    "carryislive": {"id": "carryislive", "name": "CarryisLive", "platform": "youtube", "url": "https://www.youtube.com/@CarryisLive/live", "enabled": True},
    "technogamerz": {"id": "technogamerz", "name": "Techno Gamerz", "platform": "youtube", "url": "https://www.youtube.com/@TechnoGamerzOfficial/live", "enabled": True}
}

streamers_lock, marks_lock = threading.RLock(), threading.RLock()
master_polling, disk_guard_active = False, False
CLIP_CACHE: dict[str, dict] = {}

def save_json_atomic(filepath: Path, data: any, lock: threading.Lock):
    with lock:
        tmp_file = filepath.with_suffix(".tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_file, filepath)

def load_json_safe(filepath: Path, default_data: any, lock: threading.Lock) -> any:
    with lock:
        if not filepath.exists():
            tmp = filepath.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(default_data, f, indent=2)
            os.replace(tmp, filepath)
            return default_data
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default_data

def validate_streamer_id(sid: str):
    if not sid or not STREAMER_ID_REGEX.match(sid):
        raise HTTPException(status_code=400, detail="Invalid streamer ID. Must match ^[a-z0-9_-]{1,40}$")

def validate_stream_url(url: str):
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError
        netloc = parsed.netloc.lower().split(":")[0]
        if not any(netloc == d or netloc.endswith("." + d) for d in ALLOWED_DOMAINS):
            raise ValueError
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid stream URL domain. Allowed: youtube.com, twitch.tv, kick.com")

def sanitize_clip_path(filename: str) -> Path:
    if not filename or not re.match(r"^[a-zA-Z0-9_\-\.]+\.mp4$", filename) or ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid clip path")
    resolved = (CLIPS_DIR / filename).resolve()
    if resolved.parent != CLIPS_DIR.resolve() or resolved.suffix != ".mp4":
        raise HTTPException(status_code=400, detail="Invalid clip path")
    return resolved

def parse_iso_dt(iso_str: str) -> datetime:
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.astimezone().replace(tzinfo=None) if dt.tzinfo is not None else dt
    except Exception:
        raise HTTPException(status_code=400, detail=f"Invalid ISO datetime string: '{iso_str}'")

def get_free_disk_gb() -> float:
    try:
        _, _, free = shutil.disk_usage(STREAMS_DIR)
        return free / (1024 ** 3)
    except Exception:
        return 999.0

def get_streamer_segments(sid: str) -> list[tuple[Path, datetime]]:
    stream_dir = STREAMS_DIR / sid
    if not stream_dir.exists():
        return []
    segments = []
    for entry in stream_dir.iterdir():
        if entry.is_file() and entry.suffix == ".ts" and entry.stat().st_size > 188:
            m = TS_PATTERN.match(entry.name)
            if m:
                try:
                    segments.append((entry, datetime.strptime(m.group(1), "%Y%m%d_%H%M%S")))
                except ValueError:
                    pass
    segments.sort(key=lambda x: x[1])
    return segments

# --- Process Runtime Management ---
class StreamerRuntime:
    def __init__(self, sid: str):
        self.id = sid
        self.status = "OFFLINE"
        self.p1: Optional[subprocess.Popen] = None
        self.p2: Optional[subprocess.Popen] = None
        self.log_file = None
        self.last_segment_time: float = 0.0
        self.backoff_attempt: int = 0
        self.next_retry_time: float = 0.0
        self.last_error: Optional[str] = None
        self.user_stopped: bool = False

active_streamers: dict[str, StreamerRuntime] = {}

def start_streamer(sid: str, streamers_dict: dict):
    if not streamers_dict.get(sid, {}).get("enabled", True):
        return
    rt = active_streamers.setdefault(sid, StreamerRuntime(sid))
    if rt.p2 and rt.p2.poll() is None:
        return
    url = streamers_dict[sid]["url"]
    stream_dir = STREAMS_DIR / sid
    stream_dir.mkdir(parents=True, exist_ok=True)
    out_pattern = str(stream_dir / "%Y%m%d_%H%M%S.ts")
    log_file = open(LOGS_DIR / f"{sid}.log", "ab")
    rt.log_file = log_file

    p1_cmd = ["streamlink", "--stdout", url, "best"]
    p2_cmd = ["ffmpeg", "-y", "-i", "pipe:0", "-c", "copy", "-f", "segment", "-segment_time", "6", "-segment_format", "mpegts", "-reset_timestamps", "1", "-strftime", "1", out_pattern]
    try:
        p1 = subprocess.Popen(p1_cmd, stdout=subprocess.PIPE, stderr=log_file, start_new_session=True)
        try:
            p2 = subprocess.Popen(p2_cmd, stdin=p1.stdout, stdout=subprocess.DEVNULL, stderr=log_file, start_new_session=True)
        except Exception:
            try:
                os.killpg(os.getpgid(p1.pid), signal.SIGTERM)
            except Exception:
                pass
            raise
        p1.stdout.close()
        rt.p1, rt.p2, rt.status = p1, p2, "RECORDING"
        rt.last_segment_time = time.time()
        (LOGS_DIR / f"{sid}.pid").write_text(f"{p1.pid},{p2.pid}")
    except Exception as e:
        rt.status, rt.last_error = "ERROR", str(e)
        if rt.log_file:
            try:
                rt.log_file.close()
            except Exception:
                pass

def stop_streamer(sid: str):
    if sid in active_streamers:
        rt = active_streamers[sid]
        for p in (rt.p1, rt.p2):
            if p and p.poll() is None:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    pass
        if rt.log_file:
            try:
                rt.log_file.close()
            except Exception:
                pass
            rt.log_file = None
        rt.p1, rt.p2, rt.status = None, None, "OFFLINE"
    pid_file = LOGS_DIR / f"{sid}.pid"
    if pid_file.exists():
        pid_file.unlink(missing_ok=True)

# --- Clipper Engine ---
def make_clip(streamer_id: str, start_dt: datetime, end_dt: datetime, kind: str) -> dict:
    t_start = time.perf_counter()
    segments = get_streamer_segments(streamer_id)
    if not segments:
        raise ValueError(f"No recorded segments available for streamer '{streamer_id}'")

    warnings = []
    now = datetime.now()
    if end_dt > now:
        warnings.append(f"end_dt clamped to current time {now.strftime('%H:%M:%S')}")
        end_dt = now

    padded_start = start_dt - timedelta(seconds=3)
    oldest_dt = segments[0][1]
    if padded_start < oldest_dt:
        warnings.append(f"start_dt clamped to oldest segment {oldest_dt.strftime('%H:%M:%S')}")
        padded_start = oldest_dt

    if padded_start >= end_dt:
        raise ValueError("Invalid time range: start time must be before end time")

    start_idx = 0
    for i, (_, dt) in enumerate(segments):
        if dt <= padded_start:
            start_idx = i
        else:
            break

    end_idx = start_idx
    for i in range(start_idx, len(segments)):
        end_idx = i
        if segments[i][1] >= end_dt:
            break

    selected = segments[start_idx : end_idx + 1]
    if not selected:
        raise ValueError("Could not find matching segments for time window")

    t_base = selected[0][1]
    ss_offset = max(0.0, (padded_start - t_base).total_seconds())
    to_offset = max(ss_offset + 1.0, (end_dt - t_base).total_seconds())

    base_name = f"{streamer_id}_{start_dt.strftime('%Y%m%d_%H%M%S')}_{kind}"
    candidate_name = f"{base_name}.mp4"
    counter = 1
    target_path = CLIPS_DIR / candidate_name
    while target_path.exists():
        candidate_name = f"{base_name}_{counter}.mp4"
        target_path = CLIPS_DIR / candidate_name
        counter += 1

    temp_list_path = LOGS_DIR / f"temp_concat_{streamer_id}_{uuid.uuid4().hex[:8]}.txt"
    try:
        with open(temp_list_path, "w", encoding="utf-8") as f:
            for seg_path, _ in selected:
                abs_p = seg_path.resolve().as_posix().replace("'", "'\\''")
                f.write(f"file '{abs_p}'\n")

        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(temp_list_path), "-ss", f"{ss_offset:.3f}", "-to", f"{to_offset:.3f}", "-c", "copy", "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(target_path)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"FFmpeg slicing failed: {proc.stderr[:300]}")
    finally:
        if temp_list_path.exists():
            try:
                temp_list_path.unlink()
            except OSError:
                pass

    cut_time = time.perf_counter() - t_start
    duration_sec = round(to_offset - ss_offset, 2)
    logger.info(f"Clip cut completed: {candidate_name} in {cut_time:.3f}s (target: ~2s)")

    clip_info = {
        "filename": candidate_name, "streamer_id": streamer_id,
        "title": f"{streamer_id} {start_dt.strftime('%Y-%m-%d %H:%M:%S')} ({int(duration_sec)}s)",
        "url": f"/clips/{candidate_name}", "size_bytes": target_path.stat().st_size if target_path.exists() else 0,
        "duration_sec": duration_sec, "cut_time_sec": round(cut_time, 3), "created_at": datetime.now().isoformat(),
        "warning": "; ".join(warnings) if warnings else None, "warnings": warnings
    }
    CLIP_CACHE[candidate_name] = clip_info
    return clip_info

# --- Background Watchdog & Retention Tasks ---
async def watchdog_task():
    global disk_guard_active
    delays = [600, 600, 600]
    try:
        while True:
            await asyncio.sleep(5)
            free_gb = get_free_disk_gb()
            if free_gb < 5.0:
                disk_guard_active = True
                logger.critical("Disk Guard active: Free disk space below 5 GB threshold!")
                for sid in list(active_streamers.keys()):
                    stop_streamer(sid)
                    active_streamers[sid].status = "HALTED_DISK_FULL"
                continue
            else:
                disk_guard_active = False

            streamers = load_json_safe(STREAMERS_FILE, DEFAULT_STREAMERS, streamers_lock)
            now = time.time()
            for sid, conf in streamers.items():
                if not conf.get("enabled", True):
                    if sid in active_streamers and active_streamers[sid].status != "OFFLINE":
                        stop_streamer(sid)
                    continue

                rt = active_streamers.setdefault(sid, StreamerRuntime(sid))
                if rt.user_stopped:
                    continue
                if rt.p2 and rt.p2.poll() is None:
                    segs = get_streamer_segments(sid)
                    if segs:
                        last_mtime = segs[-1][0].stat().st_mtime
                        if last_mtime > rt.last_segment_time:
                            rt.last_segment_time, rt.backoff_attempt, rt.status = last_mtime, 0, "RECORDING"
                    if rt.status == "RECORDING" and (now - rt.last_segment_time) > 30:
                        logger.warning(f"Watchdog: {sid} stalled >30s. Restarting.")
                        stop_streamer(sid)
                        rt.status, rt.last_error = "ERROR", "Watchdog stall >30s"
                        rt.backoff_attempt = min(rt.backoff_attempt + 1, 3)
                        rt.next_retry_time = now + delays[rt.backoff_attempt - 1] + random.uniform(0, 3)
                else:
                    if master_polling:
                        if rt.p2 is not None and rt.p2.poll() is not None:
                            rt.backoff_attempt = min(rt.backoff_attempt + 1, 3)
                            rt.next_retry_time = now + delays[rt.backoff_attempt - 1] + random.uniform(0, 3)
                            stop_streamer(sid)
                            rt.status = "OFFLINE"
                        if now >= rt.next_retry_time:
                            start_streamer(sid, streamers)
    except asyncio.CancelledError:
        pass

async def retention_task():
    try:
        while True:
            await asyncio.sleep(60)
            cutoff = datetime.now() - timedelta(minutes=120)
            if STREAMS_DIR.exists():
                for s_dir in STREAMS_DIR.iterdir():
                    if s_dir.is_dir():
                        for f in s_dir.glob("*.ts"):
                            m = TS_PATTERN.match(f.name)
                            if m:
                                try:
                                    if datetime.strptime(m.group(1), "%Y%m%d_%H%M%S") < cutoff:
                                        f.unlink(missing_ok=True)
                                except Exception:
                                    pass
    except asyncio.CancelledError:
        pass

# --- Lifespan & Application Setup ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    if LOGS_DIR.exists():
        for pid_file in LOGS_DIR.glob("*.pid"):
            try:
                for pid_str in pid_file.read_text().strip().split(","):
                    if pid_str.isdigit():
                        try:
                            os.killpg(os.getpgid(int(pid_str)), signal.SIGTERM)
                        except (ProcessLookupError, OSError):
                            pass
                pid_file.unlink(missing_ok=True)
            except Exception:
                pass
    load_json_safe(STREAMERS_FILE, DEFAULT_STREAMERS, streamers_lock)
    load_json_safe(MARKS_FILE, [], marks_lock)
    for entry in CLIPS_DIR.glob("*.mp4"):
        if entry.is_file() and entry.name not in CLIP_CACHE:
            try:
                st = entry.stat()
                parts = entry.stem.split("_")
                sid = parts[0] if parts else "unknown"
                CLIP_CACHE[entry.name] = {
                    "filename": entry.name, "streamer_id": sid, "title": f"{sid} {entry.stem}",
                    "url": f"/clips/{entry.name}", "size_bytes": st.st_size, "duration_sec": 0.0,
                    "cut_time_sec": 0.0, "created_at": datetime.fromtimestamp(st.st_mtime).isoformat(),
                    "warning": None, "warnings": []
                }
            except Exception:
                pass
    t1, t2 = None, None
    if not IS_SERVERLESS:
        t1 = asyncio.create_task(watchdog_task())
        t2 = asyncio.create_task(retention_task())
    yield
    if t1 and t2:
        t1.cancel()
        t2.cancel()
        await asyncio.gather(t1, t2, return_exceptions=True)
    for sid in list(active_streamers.keys()):
        stop_streamer(sid)

app = FastAPI(title="GTA6-ZeroHour Clipper", lifespan=lifespan)

# --- ASGI Security Middleware ---
@app.middleware("http")
async def security_middleware(request: Request, call_next):
    if "*" not in ALLOWED_HOSTS:
        host = request.headers.get("host")
        if not host or (host not in ALLOWED_HOSTS and host.split(":")[0] not in ALLOWED_HOSTS):
            return JSONResponse({"detail": "Forbidden host"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin not in ALLOWED_ORIGINS:
            return JSONResponse({"detail": "Forbidden origin"}, status_code=403)
    return await call_next(request)

# --- Request Models ---
class PollRequest(BaseModel):
    streamer_id: Optional[str] = None
class StreamerCreate(BaseModel):
    url: str; id: Optional[str] = None; name: Optional[str] = None; platform: Optional[str] = None; enabled: bool = True
class StreamerPatch(BaseModel):
    name: Optional[str] = None; platform: Optional[str] = None; url: Optional[str] = None; enabled: Optional[bool] = None
class LookbackRequest(BaseModel):
    streamer_id: str; seconds: int
class RangeRequest(BaseModel):
    streamer_id: str; start_iso: str; end_iso: str
class MarkCreate(BaseModel):
    streamer_id: Optional[str] = "all"; label: Optional[str] = "Mark"
class MarkClipRequest(BaseModel):
    mark_id: str; streamer_id: Optional[str] = None; pre_sec: int = 30; post_sec: int = 30
class RevealRequest(BaseModel):
    filename: str

# --- REST Endpoints ---
@app.get("/api/status")
async def get_status():
    streamers = load_json_safe(STREAMERS_FILE, DEFAULT_STREAMERS, streamers_lock)
    free_gb = get_free_disk_gb()
    try:
        total, used, free = shutil.disk_usage(STREAMS_DIR)
        disk_info = {"total_gb": round(total / (1024 ** 3), 2), "used_gb": round(used / (1024 ** 3), 2), "free_gb": round(free / (1024 ** 3), 2), "disk_guard": disk_guard_active or (free_gb < 5.0)}
    except Exception:
        disk_info = {"total_gb": 0.0, "used_gb": 0.0, "free_gb": round(free_gb, 2), "disk_guard": False}

    active_cnt, streamer_statuses = 0, []
    for sid, conf in streamers.items():
        rt = active_streamers.get(sid)
        segs = get_streamer_segments(sid)
        buf_sec = max(0.0, round((segs[-1][1] - segs[0][1]).total_seconds() + 6.0, 1)) if segs else 0.0
        oldest_s = segs[0][1].isoformat() if segs else None
        newest_s = segs[-1][1].isoformat() if segs else None

        is_running = rt and rt.p2 and rt.p2.poll() is None
        if is_running:
            active_cnt += 1
            st, pid = rt.status, rt.p2.pid
        else:
            st, pid = (rt.status if rt else "OFFLINE"), None

        streamer_statuses.append({
            "id": sid, "name": conf.get("name", sid), "platform": conf.get("platform", "unknown"),
            "url": conf.get("url", ""), "enabled": conf.get("enabled", True), "status": st, "pid": pid,
            "buffer_sec": buf_sec, "segment_count": len(segs), "oldest_segment": oldest_s,
            "newest_segment": newest_s, "last_error": rt.last_error if rt else None
        })

    return {"status": "ok", "master_polling": master_polling, "active_recordings": active_cnt, "disk": disk_info, "streamers": streamer_statuses}

@app.post("/api/poll/start")
async def poll_start(body: Optional[PollRequest] = None):
    global master_polling
    if get_free_disk_gb() < 5.0:
        raise HTTPException(status_code=400, detail="Disk Guard active: Free space below 5 GB")
    streamers = load_json_safe(STREAMERS_FILE, DEFAULT_STREAMERS, streamers_lock)
    sid = body.streamer_id if body else None
    if not sid or sid == "all":
        master_polling = True
        for s_id, conf in streamers.items():
            if conf.get("enabled", True):
                rt = active_streamers.setdefault(s_id, StreamerRuntime(s_id))
                rt.user_stopped = False
                rt.backoff_attempt, rt.next_retry_time = 0, 0.0
                start_streamer(s_id, streamers)
        return {"status": "ok", "message": "Polling started for all streamers"}
    else:
        validate_streamer_id(sid)
        if sid not in streamers:
            raise HTTPException(status_code=404, detail=f"Streamer '{sid}' not found")
        if not streamers[sid].get("enabled", True):
            raise HTTPException(status_code=400, detail=f"Streamer '{sid}' is disabled")
        rt = active_streamers.setdefault(sid, StreamerRuntime(sid))
        rt.user_stopped = False
        rt.backoff_attempt, rt.next_retry_time = 0, 0.0
        start_streamer(sid, streamers)
        return {"status": "ok", "message": f"Polling started for {sid}"}

@app.post("/api/poll/stop")
async def poll_stop(body: Optional[PollRequest] = None):
    global master_polling
    streamers = load_json_safe(STREAMERS_FILE, DEFAULT_STREAMERS, streamers_lock)
    sid = body.streamer_id if body else None
    if not sid or sid == "all":
        master_polling = False
        for s_id in list(active_streamers.keys()):
            stop_streamer(s_id)
            active_streamers[s_id].user_stopped = True
        return {"status": "ok", "message": "Polling stopped for all streamers"}
    else:
        validate_streamer_id(sid)
        if sid not in streamers:
            raise HTTPException(status_code=404, detail=f"Streamer '{sid}' not found")
        stop_streamer(sid)
        rt = active_streamers.setdefault(sid, StreamerRuntime(sid))
        rt.user_stopped = True
        return {"status": "ok", "message": f"Polling stopped for {sid}"}

@app.get("/api/streamers")
async def list_streamers():
    return list(load_json_safe(STREAMERS_FILE, DEFAULT_STREAMERS, streamers_lock).values())

@app.post("/api/streamers", status_code=201)
async def create_streamer(body: StreamerCreate):
    clean_url = body.url.strip()
    plat = body.platform or ("twitch" if "twitch.tv" in clean_url else "kick" if "kick.com" in clean_url else "youtube")
    if plat == "youtube" and "/@" in clean_url and not clean_url.endswith("/live") and "/watch" not in clean_url:
        clean_url = clean_url.rstrip("/") + "/live"
    validate_stream_url(clean_url)
    sid = body.id
    auto_id = False
    if not sid:
        auto_id = True
        pts = [p for p in urllib.parse.urlparse(clean_url).path.strip("/").split("/") if p and p != "live"]
        raw = pts[-1].lstrip("@") if pts else "streamer"
        sid = re.sub(r"[^a-z0-9_-]", "", raw.lower())[:40] or f"stream_{int(time.time())}"
    validate_streamer_id(sid)
    with streamers_lock:
        streamers = load_json_safe(STREAMERS_FILE, DEFAULT_STREAMERS, streamers_lock)
        if sid in streamers:
            if not auto_id:
                raise HTTPException(status_code=400, detail="Streamer ID already exists")
            return {"status": "ok", "streamer": streamers[sid]}
        obj = {"id": sid, "name": body.name or sid, "platform": plat, "url": clean_url, "enabled": body.enabled}
        streamers[sid] = obj
        save_json_atomic(STREAMERS_FILE, streamers, streamers_lock)
    return {"status": "ok", "streamer": obj}

@app.patch("/api/streamers/{streamer_id}")
async def patch_streamer(streamer_id: str, body: StreamerPatch):
    validate_streamer_id(streamer_id)
    with streamers_lock:
        streamers = load_json_safe(STREAMERS_FILE, DEFAULT_STREAMERS, streamers_lock)
        if streamer_id not in streamers:
            raise HTTPException(status_code=404, detail=f"Streamer '{streamer_id}' not found")
        if body.url is not None:
            validate_stream_url(body.url)
            streamers[streamer_id]["url"] = body.url
        if body.name is not None:
            streamers[streamer_id]["name"] = body.name
        if body.platform is not None:
            streamers[streamer_id]["platform"] = body.platform
        if body.enabled is not None:
            streamers[streamer_id]["enabled"] = body.enabled
            if not body.enabled:
                stop_streamer(streamer_id)
        save_json_atomic(STREAMERS_FILE, streamers, streamers_lock)
        updated = streamers[streamer_id]
    return {"status": "ok", "streamer": updated}

@app.delete("/api/streamers/{streamer_id}")
async def delete_streamer(streamer_id: str):
    validate_streamer_id(streamer_id)
    with streamers_lock:
        streamers = load_json_safe(STREAMERS_FILE, DEFAULT_STREAMERS, streamers_lock)
        if streamer_id not in streamers:
            raise HTTPException(status_code=404, detail=f"Streamer '{streamer_id}' not found")
        stop_streamer(streamer_id)
        del streamers[streamer_id]
        save_json_atomic(STREAMERS_FILE, streamers, streamers_lock)
    return {"status": "ok", "deleted": streamer_id}

@app.post("/api/clip/lookback")
async def clip_lookback(body: LookbackRequest):
    validate_streamer_id(body.streamer_id)
    if body.seconds <= 0:
        raise HTTPException(status_code=400, detail="seconds must be greater than 0")
    now = datetime.now()
    start_dt = now - timedelta(seconds=body.seconds)
    try:
        clip_meta = await asyncio.to_thread(make_clip, body.streamer_id, start_dt, now, f"lookback_{body.seconds}s")
        return {"status": "ok", "clip": clip_meta}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/clip/range")
async def clip_range(body: RangeRequest):
    validate_streamer_id(body.streamer_id)
    start_dt, end_dt = parse_iso_dt(body.start_iso), parse_iso_dt(body.end_iso)
    if start_dt >= end_dt:
        raise HTTPException(status_code=400, detail="start_iso must be before end_iso")
    try:
        clip_meta = await asyncio.to_thread(make_clip, body.streamer_id, start_dt, end_dt, "range")
        return {"status": "ok", "clip": clip_meta}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/mark")
async def create_mark(body: Optional[MarkCreate] = None):
    sid = body.streamer_id if body and body.streamer_id else "all"
    if sid != "all":
        validate_streamer_id(sid)
    label = body.label if body and body.label else "Mark"
    mark_item = {"id": f"mark_{int(time.time())}_{uuid.uuid4().hex[:4]}", "timestamp": datetime.now().isoformat(), "streamer_id": sid, "label": label}
    with marks_lock:
        marks = load_json_safe(MARKS_FILE, [], marks_lock)
        marks.insert(0, mark_item)
        save_json_atomic(MARKS_FILE, marks, marks_lock)
    return {"status": "ok", "mark": mark_item}

@app.get("/api/marks")
async def get_marks():
    return load_json_safe(MARKS_FILE, [], marks_lock)

@app.post("/api/clip/mark")
async def clip_mark(body: MarkClipRequest):
    marks = load_json_safe(MARKS_FILE, [], marks_lock)
    found = next((m for m in marks if m["id"] == body.mark_id), None)
    if not found:
        raise HTTPException(status_code=404, detail="Mark not found")

    sid = body.streamer_id or found.get("streamer_id")
    if not sid or sid == "all":
        streamers = load_json_safe(STREAMERS_FILE, DEFAULT_STREAMERS, streamers_lock)
        candidates = [s for s in streamers if get_streamer_segments(s)]
        if not candidates:
            raise HTTPException(status_code=400, detail="No streamers with recorded buffer found for mark")
        sid = candidates[0]

    validate_streamer_id(sid)
    mark_dt = parse_iso_dt(found["timestamp"])
    start_dt = mark_dt - timedelta(seconds=max(1, body.pre_sec))
    end_dt = mark_dt + timedelta(seconds=max(1, body.post_sec))
    try:
        clip_meta = await asyncio.to_thread(make_clip, sid, start_dt, end_dt, f"mark_{body.mark_id}")
        return {"status": "ok", "clip": clip_meta}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/clips")
async def list_clips():
    current_files = {f.name for f in CLIPS_DIR.glob("*.mp4") if f.is_file()}
    for k in list(CLIP_CACHE.keys()):
        if k not in current_files:
            del CLIP_CACHE[k]
    for name in current_files:
        if name not in CLIP_CACHE:
            p = CLIPS_DIR / name
            st = p.stat()
            parts = p.stem.split("_")
            sid = parts[0] if parts else "unknown"
            CLIP_CACHE[name] = {
                "filename": name, "streamer_id": sid, "title": f"{sid} {p.stem}",
                "url": f"/clips/{name}", "size_bytes": st.st_size, "duration_sec": 0.0,
                "cut_time_sec": 0.0, "created_at": datetime.fromtimestamp(st.st_mtime).isoformat(), "warning": None, "warnings": []
            }
    return sorted(CLIP_CACHE.values(), key=lambda x: x["created_at"], reverse=True)

@app.get("/clips/{filename}")
async def get_clip_file(filename: str):
    file_path = sanitize_clip_path(filename)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Clip not found")
    return FileResponse(path=file_path, media_type="video/mp4")

@app.post("/api/reveal")
async def reveal_file(body: RevealRequest):
    file_path = sanitize_clip_path(body.filename)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Clip not found")
    try:
        subprocess.run(["open", "-R", str(file_path)], check=True)
        return {"status": "ok", "revealed": body.filename}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to reveal in Finder: {e}")

from gta6_clipper.routes_clip import router as r1; from gta6_clipper.routes_moments import router as r2; app.include_router(r1); app.include_router(r2)
if STATIC_DIR.exists(): app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
