import hashlib
import json
import logging
import math
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Dict, List, Optional, Tuple

from gta6_clipper.extractor import (
    format_seconds_to_hms,
    get_media_duration,
    parse_time_offset,
)

logger = logging.getLogger("gta6_clipper.moments")

DEFAULT_MODEL = "gemini-3.8-flash"
DEFAULT_CHUNK_MINUTES = 30.0
DEFAULT_OVERLAP_SECONDS = 30.0
DEFAULT_DELAY_SECONDS = 5.0
DEFAULT_MAX_MOMENTS = 10

def get_gemini_api_key() -> Optional[str]:
    """Retrieve GEMINI_API_KEY strictly from environment. Never log or leak."""
    return os.getenv("GEMINI_API_KEY", "").strip() or None

def get_gemini_model() -> str:
    """Retrieve configured model name defaulting to gemini-3.8-flash."""
    return os.getenv("GEMINI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL

def compute_file_hash(path: Path) -> str:
    """Compute fast content/size hash for sidecar cache validation."""
    if not path.exists():
        return ""
    h = hashlib.sha256()
    size = path.stat().st_size
    h.update(str(size).encode("utf-8"))
    try:
        with open(path, "rb") as f:
            h.update(f.read(1024 * 1024))
    except Exception:
        pass
    return h.hexdigest()[:16]

def make_proxy(source_path: Path, output_dir: Path) -> Path:
    """
    Generate lightweight 480p, 10fps, 64k aac audio proxy for efficient upload.
    Uses argument list (never shell=True).
    """
    if not source_path.exists():
        raise FileNotFoundError(f"Source video '{source_path.name}' not found")
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg binary is required on PATH")

    output_dir.mkdir(parents=True, exist_ok=True)
    proxy_path = output_dir / f"proxy_{source_path.stem}.mp4"

    cmd = [
        "ffmpeg", "-y",
        "-i", str(source_path),
        "-vf", "scale=-2:480,fps=10",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
        "-c:a", "aac", "-b:a", "64k",
        "-movflags", "+faststart",
        str(proxy_path)
    ]

    res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if res.returncode != 0 or not proxy_path.exists() or proxy_path.stat().st_size == 0:
        raise RuntimeError(f"Proxy generation failed: {res.stderr[:300]}")

    return proxy_path

def split_chunks(
    proxy_path: Path,
    chunk_minutes: float = DEFAULT_CHUNK_MINUTES,
    overlap_sec: float = DEFAULT_OVERLAP_SECONDS,
    output_dir: Optional[Path] = None,
) -> List[Tuple[Path, float]]:
    """
    Split proxy video into chunks of `chunk_minutes` with `overlap_sec` overlap.
    Returns list of (chunk_path, offset_sec).
    """
    dur = get_media_duration(proxy_path)
    chunk_sec = max(60.0, chunk_minutes * 60.0)

    if dur <= chunk_sec:
        return [(proxy_path, 0.0)]

    if output_dir is None:
        output_dir = proxy_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    chunks = []
    start = 0.0
    idx = 0

    while start < dur:
        end = min(start + chunk_sec, dur)
        chunk_file = output_dir / f"chunk_{idx}_{int(start)}_{int(end)}.mp4"

        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{start:.3f}",
            "-to", f"{end:.3f}",
            "-i", str(proxy_path),
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            str(chunk_file)
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if res.returncode != 0 or not chunk_file.exists():
            raise RuntimeError(f"Chunk splitting failed at {start:.1f}s: {res.stderr[:200]}")

        chunks.append((chunk_file, start))
        if end >= dur:
            break
        start = end - overlap_sec
        idx += 1

    return chunks

def analyse_chunk_gemini(
    client,
    chunk_path: Path,
    style_hint: Optional[str] = None,
    thinking_level: str = "low",
    max_retries: int = 3,
) -> List[Dict]:
    """
    Upload a chunk via Gemini Files API, call Gemini 3.8 Flash with structured schema,
    and guarantee file deletion in finally block.
    """
    from google.genai import types

    model_name = get_gemini_model()
    file_ref = None

    try:
        # 1. Upload chunk
        file_ref = client.files.upload(file=str(chunk_path))

        # 2. Poll until ACTIVE
        t0 = time.time()
        while True:
            f_info = client.files.get(name=file_ref.name)
            state = getattr(f_info, "state", "")
            state_str = str(state.value if hasattr(state, "value") else state).upper()
            if "ACTIVE" in state_str:
                break
            if "FAIL" in state_str:
                err = getattr(f_info, "error", "Gemini file processing error")
                raise RuntimeError(f"Gemini file state failed: {err}")
            if time.time() - t0 > 180:
                raise TimeoutError("Gemini file processing state timed out after 180s")
            time.sleep(2)

        # 3. Construct prompt & configuration
        # NOTE: Gemini Flash Thinking: Support both thinking_budget and thinking_level
        tc_kwargs = {}
        if hasattr(types, "ThinkingConfig") and hasattr(types.ThinkingConfig, "model_fields"):
            if "thinking_budget" in types.ThinkingConfig.model_fields:
                tc_kwargs["thinking_budget"] = 1024
            elif "thinking_level" in types.ThinkingConfig.model_fields:
                tc_kwargs["thinking_level"] = thinking_level if thinking_level in ("low", "medium") else "low"
        tc = types.ThinkingConfig(**tc_kwargs) if tc_kwargs else None
        cfg = types.GenerateContentConfig(
            thinking_config=tc,
            response_mime_type="application/json",
        )

        prompt = (
            "You are an expert video editor and streaming highlight analyst. "
            "Analyze this video footage and identify the best moments suitable for short viral clips (15 to 60 seconds each). "
            "Focus on high energy, funny reactions, surprising events, emotional peaks, or strong hooks. Do not output overlapping moments. "
        )
        if style_hint and style_hint.strip():
            prompt += f"Style preference: {style_hint.strip()}. "

        prompt += (
            "Return a valid JSON object matching this exact schema:\n"
            "{\n"
            '  "moments": [\n'
            '    {\n'
            '      "start": "HH:MM:SS",\n'
            '      "end": "HH:MM:SS",\n'
            '      "score": 1 to 10,\n'
            '      "title": "Short punchy title",\n'
            '      "reason": "1-sentence rationale for selection"\n'
            "    }\n"
            "  ]\n"
            "}"
        )

        # 4. Generate content with rate-limit retry and JSON validation
        last_err = None
        for attempt in range(max_retries):
            try:
                resp = client.models.generate_content(
                    model=model_name,
                    contents=[file_ref, prompt],
                    config=cfg,
                )
                text = (resp.text or "").strip()
                if not text:
                    raise ValueError("Gemini returned empty response")

                # Parse JSON
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    # Strip markdown blocks if present
                    if text.startswith("```"):
                        clean_text = text.split("```")[1]
                        if clean_text.startswith("json"):
                            clean_text = clean_text[4:]
                        data = json.loads(clean_text.strip())
                    else:
                        raise

                moments = data.get("moments", [])
                if isinstance(moments, list):
                    return moments
                raise ValueError("JSON response missing 'moments' array")

            except Exception as e:
                last_err = e
                err_str = str(e).lower()
                is_rate_limit = any(term in err_str for term in ("429", "503", "quota", "rate limit", "resource exhausted"))
                if is_rate_limit and attempt < (max_retries - 1):
                    backoff = (2 ** attempt) * 2.0
                    time.sleep(backoff)
                    continue
                if isinstance(e, (json.JSONDecodeError, ValueError)) and attempt < (max_retries - 1):
                    time.sleep(1.0)
                    continue
                raise last_err

    finally:
        # Guarantee uploaded Gemini file is deleted
        if file_ref and hasattr(file_ref, "name"):
            try:
                client.files.delete(name=file_ref.name)
            except Exception:
                pass

    return []

def merge_moments(
    moments: List[Dict],
    min_len: float = 15.0,
    max_len: float = 60.0,
    max_moments: int = DEFAULT_MAX_MOMENTS,
) -> List[Dict]:
    """
    Merge overlapping moments (>50% overlap keeps higher score),
    filter by duration bounds, and return top MAX_MOMENTS sorted by start time.
    """
    valid = []
    for m in moments:
        s = float(m.get("start_sec", 0.0))
        e = float(m.get("end_sec", 0.0))
        dur = e - s
        if dur < (min_len - 1.0) or dur > (max_len + 2.0):
            continue
        score = int(m.get("score", 5))
        valid.append({
            "start": m.get("start_hms", format_seconds_to_hms(s)),
            "end": m.get("end_hms", format_seconds_to_hms(e)),
            "start_sec": round(s, 2),
            "end_sec": round(e, 2),
            "duration_sec": round(dur, 2),
            "score": max(1, min(10, score)),
            "title": str(m.get("title", "Highlight")).strip(),
            "reason": str(m.get("reason", "")).strip(),
        })

    if not valid:
        return []

    # Sort primarily by score descending, secondarily by duration
    valid.sort(key=lambda x: (x["score"], -x["duration_sec"]), reverse=True)

    retained: List[Dict] = []
    for candidate in valid:
        c_start, c_end = candidate["start_sec"], candidate["end_sec"]
        c_dur = c_end - c_start
        has_major_overlap = False

        for kept in retained:
            k_start, k_end = kept["start_sec"], kept["end_sec"]
            ov_start = max(c_start, k_start)
            ov_end = min(c_end, k_end)
            ov_len = max(0.0, ov_end - ov_start)

            shorter_len = min(c_dur, kept["duration_sec"])
            if shorter_len > 0 and (ov_len / shorter_len) > 0.50:
                has_major_overlap = True
                break

        if not has_major_overlap:
            retained.append(candidate)

    # Pick top N by score
    top_n = retained[:max_moments]

    # Return final results sorted chronologically by start_sec
    top_n.sort(key=lambda x: x["start_sec"])
    return top_n

def load_cached_moments(
    clip_path: Path,
    model: str,
    params: Dict,
) -> Optional[List[Dict]]:
    """Load sidecar cache if file hash, model, and parameters match."""
    sidecar_path = clip_path.with_suffix(clip_path.suffix + ".moments.json")
    if not sidecar_path.exists():
        return None

    try:
        data = json.loads(sidecar_path.read_text(encoding="utf-8"))
        curr_hash = compute_file_hash(clip_path)
        if data.get("file_hash") != curr_hash:
            return None
        if data.get("model") != model:
            return None
        cached_params = data.get("params", {})
        for k in ("max_moments", "min_len", "max_len", "style_hint"):
            if cached_params.get(k) != params.get(k):
                return None
        return data.get("moments", [])
    except Exception:
        return None

def save_cached_moments(
    clip_path: Path,
    model: str,
    params: Dict,
    moments: List[Dict],
) -> Path:
    """Save analysis results to sidecar cache <clip>.moments.json."""
    sidecar_path = clip_path.with_suffix(clip_path.suffix + ".moments.json")
    payload = {
        "file_hash": compute_file_hash(clip_path),
        "model": model,
        "params": params,
        "moments": moments,
        "analyzed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    tmp_path = sidecar_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp_path.replace(sidecar_path)
    return sidecar_path

def estimate_job(file_path: Path) -> Dict:
    """Non-network estimation of video chunks and token consumption."""
    if not file_path.exists():
        raise FileNotFoundError(f"Clip '{file_path.name}' not found")

    dur = get_media_duration(file_path)
    chunk_min = float(os.getenv("CHUNK_MINUTES", str(DEFAULT_CHUNK_MINUTES)))
    chunk_sec = max(60.0, chunk_min * 60.0)
    chunk_count = max(1, math.ceil(dur / chunk_sec))
    approx_tokens = int(dur * 260)  # ~260 tokens per second for audio+video downsampled

    return {
        "filename": file_path.name,
        "duration_sec": round(dur, 2),
        "duration_hms": format_seconds_to_hms(dur),
        "chunk_minutes": chunk_min,
        "chunk_count": chunk_count,
        "approx_tokens": approx_tokens,
        "estimated_api_calls": chunk_count,
        "model": get_gemini_model(),
    }
