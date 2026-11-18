import asyncio
from datetime import datetime
import os
from pathlib import Path
import time
from typing import Literal, Optional
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from gta6_clipper.extractor import (
    MAX_EXTRACT_SECONDS,
    extract_stream_section,
    format_seconds_to_hms,
    get_media_duration,
    parse_time_offset,
    trim_clip,
)

router = APIRouter(tags=["clipping"])

class ExtractStreamRequest(BaseModel):
    streamer_id: Optional[str] = None
    url: Optional[str] = None
    start_time: str
    end_time: Optional[str] = None
    duration: Optional[str] = None
    title: Optional[str] = None

class TrimClipRequest(BaseModel):
    filename: str
    start_sec: float
    end_sec: float
    mode: Literal["fast", "exact"] = "fast"
    title_suffix: str = "trim"


@router.post("/api/clip/extract")
async def clip_extract(body: ExtractStreamRequest):
    # Import shared dependencies from app lazily to prevent circular imports
    from gta6_clipper.app import (
        CLIP_CACHE,
        CLIPS_DIR,
        DEFAULT_STREAMERS,
        STREAMERS_FILE,
        load_json_safe,
        streamers_lock,
        validate_stream_url,
    )

    # 1. Resolve stream URL
    target_url = None
    sid_label = "custom"
    if body.streamer_id:
        streamers = load_json_safe(STREAMERS_FILE, DEFAULT_STREAMERS, streamers_lock)
        if body.streamer_id not in streamers:
            raise HTTPException(status_code=404, detail=f"Streamer '{body.streamer_id}' not found in roster")
        target_url = streamers[body.streamer_id].get("url")
        sid_label = body.streamer_id
    elif body.url:
        target_url = body.url.strip()
    else:
        raise HTTPException(status_code=400, detail="Either streamer_id or url must be provided")

    if not target_url or target_url.startswith("-"):
        raise HTTPException(status_code=400, detail="Invalid stream URL")

    # 2. Validate URL domain
    validate_stream_url(target_url)

    # 3. Parse start and end timestamps
    try:
        start_sec = parse_time_offset(body.start_time)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid start_time: {e}")

    if body.end_time:
        try:
            end_sec = parse_time_offset(body.end_time)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Invalid end_time: {e}")
    elif body.duration:
        try:
            dur_sec = parse_time_offset(body.duration)
            end_sec = start_sec + dur_sec
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Invalid duration: {e}")
    else:
        raise HTTPException(status_code=400, detail="Either end_time or duration must be provided")

    max_sec = float(os.getenv("MAX_EXTRACT_SECONDS", str(MAX_EXTRACT_SECONDS)))
    if start_sec < 0 or end_sec <= start_sec:
        raise HTTPException(status_code=400, detail="start_time must be >= 0 and less than end_time")
    if (end_sec - start_sec) > max_sec:
        raise HTTPException(
            status_code=400,
            detail=f"Extraction window ({end_sec - start_sec:.1f}s) exceeds maximum allowed ({max_sec:.0f}s)"
        )

    # 4. Determine unique output file path
    CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    ts_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_name = f"{sid_label}_{ts_stamp}_extract"
    candidate_name = f"{base_name}.mp4"
    counter = 1
    target_path = CLIPS_DIR / candidate_name
    while target_path.exists():
        candidate_name = f"{base_name}_{counter}.mp4"
        target_path = CLIPS_DIR / candidate_name
        counter += 1

    # 5. Execute extraction via thread
    t_start = time.perf_counter()
    try:
        res = await asyncio.to_thread(extract_stream_section, target_url, start_sec, end_sec, target_path)
    except RuntimeError as e:
        err_msg = str(e)
        if "VOD required for this platform" in err_msg:
            raise HTTPException(status_code=400, detail="VOD required for this platform")
        raise HTTPException(status_code=500, detail=err_msg)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Extraction failed: {e}")

    cut_time = time.perf_counter() - t_start
    final_title = body.title.strip() if body.title and body.title.strip() else f"{sid_label} {format_seconds_to_hms(start_sec)}-{format_seconds_to_hms(end_sec)}"

    clip_info = {
        "filename": candidate_name,
        "streamer_id": sid_label,
        "title": final_title,
        "url": f"/clips/{candidate_name}",
        "size_bytes": res.get("size", target_path.stat().st_size if target_path.exists() else 0),
        "duration_sec": round(res.get("duration", end_sec - start_sec), 2),
        "cut_time_sec": round(cut_time, 3),
        "created_at": datetime.now().isoformat(),
        "warning": None,
        "warnings": []
    }
    CLIP_CACHE[candidate_name] = clip_info
    return {"status": "ok", "clip": clip_info}


@router.post("/api/clip/trim")
async def clip_trim(body: TrimClipRequest):
    # Import shared dependencies from app lazily
    from gta6_clipper.app import CLIP_CACHE, CLIPS_DIR, sanitize_clip_path

    # 1. Validate range bounds
    if body.start_sec < 0 or body.end_sec <= body.start_sec:
        raise HTTPException(status_code=400, detail="start_sec must be >= 0 and less than end_sec")

    # 2. Validate filename
    source_path = sanitize_clip_path(body.filename)
    if not source_path.exists():
        raise HTTPException(status_code=404, detail="Source clip not found")

    # 3. Validate range bounds against source duration
    source_dur = get_media_duration(source_path)

    if source_dur > 0 and body.end_sec > (source_dur + 1.5):
        raise HTTPException(
            status_code=400,
            detail=f"end_sec ({body.end_sec:.1f}s) exceeds clip duration ({source_dur:.1f}s)"
        )

    # 3. Determine unique output path
    stem = source_path.stem
    clean_suffix = body.title_suffix.strip() if body.title_suffix else "trim"
    clean_suffix = "".join(c for c in clean_suffix if c.isalnum() or c in ("_", "-"))
    if not clean_suffix:
        clean_suffix = "trim"

    candidate_name = f"{stem}_{clean_suffix}.mp4"
    counter = 1
    target_path = CLIPS_DIR / candidate_name
    while target_path.exists():
        candidate_name = f"{stem}_{clean_suffix}_{counter}.mp4"
        target_path = CLIPS_DIR / candidate_name
        counter += 1

    # 4. Execute trimming
    t_start = time.perf_counter()
    try:
        res = await asyncio.to_thread(trim_clip, source_path, body.start_sec, body.end_sec, target_path, body.mode)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Trim failed: {e}")

    cut_time = time.perf_counter() - t_start

    # Inherit streamer_id and title if present in cache
    orig_info = CLIP_CACHE.get(body.filename, {})
    sid = orig_info.get("streamer_id", stem.split("_")[0])
    orig_title = orig_info.get("title", stem)
    trim_title = f"{orig_title} (Trim {format_seconds_to_hms(body.start_sec)}-{format_seconds_to_hms(body.end_sec)})"

    clip_info = {
        "filename": candidate_name,
        "streamer_id": sid,
        "title": trim_title,
        "url": f"/clips/{candidate_name}",
        "size_bytes": res.get("size", target_path.stat().st_size if target_path.exists() else 0),
        "duration_sec": round(res.get("duration", body.end_sec - body.start_sec), 2),
        "cut_time_sec": round(cut_time, 3),
        "created_at": datetime.now().isoformat(),
        "warning": None,
        "warnings": []
    }
    CLIP_CACHE[candidate_name] = clip_info
    return {"status": "ok", "clip": clip_info}
