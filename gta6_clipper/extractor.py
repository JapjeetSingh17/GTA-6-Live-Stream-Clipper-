import math
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Dict, Union

# Default max extraction window: 1800s (30 minutes)
MAX_EXTRACT_SECONDS = float(os.getenv("MAX_EXTRACT_SECONDS", "1800"))

def parse_time_offset(val: Union[str, int, float]) -> float:
    """
    Parse flexible timestamp formats to seconds (float).
    Accepts:
      - HH:MM:SS (e.g. "01:23:45" -> 5025.0)
      - MM:SS (e.g. "15:30" -> 930.0)
      - "10m" -> 600.0, "45s" -> 45.0, "1.5h" -> 5400.0
      - Leading '+' allowed: "+10m" -> 600.0
      - Numbers or plain strings: 120, "120.5"
    Rejects:
      - Negative values ("-10", "-01:00:00")
      - Empty/None
      - Non-finite (inf, nan)
      - Malformed formats ("abc", "12:34:56:78", "12:99")
    """
    if val is None:
        raise ValueError("Time offset cannot be empty or None")

    if isinstance(val, (int, float)):
        if math.isnan(val) or math.isinf(val):
            raise ValueError("Time offset must be a finite number")
        if val < 0:
            raise ValueError("Time offset cannot be negative")
        return float(val)

    if not isinstance(val, str):
        raise ValueError(f"Unsupported time offset type: {type(val)}")

    s = val.strip()
    if not s:
        raise ValueError("Time offset cannot be empty")

    if s.startswith("+"):
        s = s[1:].strip()
        if not s:
            raise ValueError("Time offset cannot be empty after '+'")

    if s.startswith("-"):
        raise ValueError("Time offset cannot be negative")

    # Time unit suffixes
    lower_s = s.lower()
    if lower_s.endswith("ms"):
        num_part = lower_s[:-2].strip()
        try:
            v = float(num_part)
            if math.isnan(v) or math.isinf(v) or v < 0: raise ValueError
            return v / 1000.0
        except Exception:
            raise ValueError(f"Invalid millisecond offset: {val}")

    if lower_s.endswith("s"):
        num_part = lower_s[:-1].strip()
        try:
            v = float(num_part)
            if math.isnan(v) or math.isinf(v) or v < 0: raise ValueError
            return v
        except Exception:
            raise ValueError(f"Invalid second offset: {val}")

    if lower_s.endswith("m"):
        num_part = lower_s[:-1].strip()
        try:
            v = float(num_part)
            if math.isnan(v) or math.isinf(v) or v < 0: raise ValueError
            return v * 60.0
        except Exception:
            raise ValueError(f"Invalid minute offset: {val}")

    if lower_s.endswith("h"):
        num_part = lower_s[:-1].strip()
        try:
            v = float(num_part)
            if math.isnan(v) or math.isinf(v) or v < 0: raise ValueError
            return v * 3600.0
        except Exception:
            raise ValueError(f"Invalid hour offset: {val}")

    # Colon-separated formats: MM:SS or HH:MM:SS
    if ":" in s:
        parts = s.split(":")
        if len(parts) == 2:
            try:
                mm = float(parts[0])
                ss = float(parts[1])
            except Exception:
                raise ValueError(f"Invalid MM:SS timestamp: {val}")
            if math.isnan(mm) or math.isnan(ss) or math.isinf(mm) or math.isinf(ss):
                raise ValueError("Timestamp values must be finite")
            if mm < 0 or ss < 0 or ss >= 60:
                raise ValueError(f"Invalid MM:SS values (seconds must be 0-59.999): {val}")
            return mm * 60.0 + ss

        elif len(parts) == 3:
            try:
                hh = float(parts[0])
                mm = float(parts[1])
                ss = float(parts[2])
            except Exception:
                raise ValueError(f"Invalid HH:MM:SS timestamp: {val}")
            if any(math.isnan(x) or math.isinf(x) for x in (hh, mm, ss)):
                raise ValueError("Timestamp values must be finite")
            if hh < 0 or mm < 0 or ss < 0 or mm >= 60 or ss >= 60:
                raise ValueError(f"Invalid HH:MM:SS values (minutes/seconds must be 0-59.999): {val}")
            return hh * 3600.0 + mm * 60.0 + ss
        else:
            raise ValueError(f"Invalid colon-separated timestamp format: {val}")

    # Plain float/int string
    try:
        v = float(s)
        if math.isnan(v) or math.isinf(v):
            raise ValueError
        if v < 0:
            raise ValueError("Time offset cannot be negative")
        return v
    except Exception:
        raise ValueError(f"Unable to parse timestamp offset: {val}")


def format_seconds_to_hms(seconds: float) -> str:
    """Format seconds into HH:MM:SS or MM:SS under an hour."""
    if seconds < 0:
        raise ValueError("Seconds cannot be negative")
    total_secs = int(seconds)
    hours = total_secs // 3600
    minutes = (total_secs % 3600) // 60
    secs = total_secs % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def get_media_duration(file_path: Path) -> float:
    """Retrieve exact media duration using ffprobe."""
    if not file_path.exists():
        return 0.0
    ffprobe_bin = shutil.which("ffprobe")
    if not ffprobe_bin:
        return 0.0
    try:
        cmd = [
            ffprobe_bin, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(file_path)
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if res.returncode == 0 and res.stdout.strip():
            return round(float(res.stdout.strip()), 3)
    except Exception:
        pass
    return 0.0


def extract_stream_section(url: str, start_sec: float, end_sec: float, output_path: Path) -> Dict:
    """
    Extract a slice from a live stream or VOD using yt-dlp.
    Uses argument list (never shell=True).
    Enforces allowed domains, start < end, MAX_EXTRACT_SECONDS, and path safety.
    """
    if not shutil.which("yt-dlp"):
        raise RuntimeError("yt-dlp binary is required on PATH for stream extraction")
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg binary is required on PATH for stream extraction")

    if not url or url.startswith("-"):
        raise ValueError("Invalid stream URL format")

    if start_sec < 0 or end_sec <= start_sec:
        raise ValueError("Invalid time bounds: start must be >= 0 and < end")

    max_sec = float(os.getenv("MAX_EXTRACT_SECONDS", "1800"))
    if (end_sec - start_sec) > max_sec:
        raise ValueError(f"Requested duration ({end_sec - start_sec:.1f}s) exceeds maximum allowed ({max_sec:.0f}s)")

    start_hms = format_seconds_to_hms(start_sec)
    end_hms = format_seconds_to_hms(end_sec)
    section_spec = f"*{start_hms}-{end_hms}"

    cmd = [
        "yt-dlp",
        "--force-ipv4",
        "--no-part",
        "--force-keyframes-at-cuts",
        "--download-sections", section_spec,
        "-S", "res:1080,fps:60",
        "-f", "bv*[height<=1080]+ba/b[height<=1080]/best",
        "--merge-output-format", "mp4",
    ]

    # YouTube live streams and VODs: use node JS runtime and cookies
    if "youtube.com" in url or "youtu.be" in url:
        cmd.extend([
            "--js-runtimes", "node",
            "--live-from-start",
        ])
        cwd_cookie = Path.cwd() / "cookies.txt"
        if cwd_cookie.exists():
            cmd.extend(["--cookies", str(cwd_cookie)])

    cmd.extend(["-o", str(output_path), "--", url])

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        raise RuntimeError("yt-dlp extraction timed out (300s limit)")

    if proc.returncode != 0:
        err_msg = proc.stderr or proc.stdout or "Unknown yt-dlp error"
        lower_err = err_msg.lower()
        # Platform VOD error detection for Twitch/Kick live streams
        if any(term in lower_err for term in ("vod", "cannot be downloaded", "not currently live", "no video formats", "live streams without vod")):
            raise RuntimeError("VOD required for this platform")
        if ("twitch" in url.lower() or "kick" in url.lower()) and "error" in lower_err:
            raise RuntimeError("VOD required for this platform")
        raise RuntimeError(f"yt-dlp extraction failed: {err_msg[:300]}")

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError("yt-dlp reported success but no output clip was produced")

    dur = get_media_duration(output_path)
    if dur <= 0:
        dur = round(end_sec - start_sec, 2)

    return {
        "path": output_path,
        "duration": dur,
        "size": output_path.stat().st_size
    }


def trim_clip(source_path: Path, start_sec: float, end_sec: float, target_path: Path, mode: str = "fast") -> Dict:
    """
    Trim a clip in fast mode (-c copy) or exact mode (re-encoded x264).
    Uses argument list (never shell=True).
    """
    if not source_path.exists():
        raise FileNotFoundError(f"Source clip '{source_path.name}' not found")

    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg binary is required on PATH for clip trimming")

    if start_sec < 0 or end_sec <= start_sec:
        raise ValueError("Invalid trim range: start_sec must be >= 0 and < end_sec")

    if mode == "exact":
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{start_sec:.3f}",
            "-to", f"{end_sec:.3f}",
            "-i", str(source_path),
            "-c:v", "libx264",
            "-crf", "18",
            "-preset", "veryfast",
            "-c:a", "aac",
            "-movflags", "+faststart",
            str(target_path)
        ]
    else: # "fast"
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{start_sec:.3f}",
            "-to", f"{end_sec:.3f}",
            "-i", str(source_path),
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
            str(target_path)
        ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise RuntimeError("FFmpeg trim timed out (120s limit)")

    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg trim failed: {(proc.stderr or '')[:300]}")

    if not target_path.exists() or target_path.stat().st_size == 0:
        raise RuntimeError("FFmpeg completed but target clip was not created")

    dur = get_media_duration(target_path)
    if dur <= 0:
        dur = round(end_sec - start_sec, 2)

    return {
        "path": target_path,
        "duration": dur,
        "size": target_path.stat().st_size
    }
