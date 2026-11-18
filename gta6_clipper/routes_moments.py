import asyncio
from datetime import datetime
import logging
import os
from pathlib import Path
import shutil
import tempfile
import time
from typing import Dict, Literal, Optional
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from gta6_clipper.extractor import (
    format_seconds_to_hms,
    get_media_duration,
    parse_time_offset,
)
from gta6_clipper.moments import (
    DEFAULT_CHUNK_MINUTES,
    DEFAULT_DELAY_SECONDS,
    DEFAULT_MAX_MOMENTS,
    analyse_chunk_gemini,
    estimate_job,
    get_gemini_api_key,
    get_gemini_model,
    load_cached_moments,
    make_proxy,
    merge_moments,
    save_cached_moments,
    split_chunks,
)
from gta6_clipper.openrouter import (
    get_openrouter_api_key,
    get_openrouter_model,
)
from gta6_clipper.resolve_export import export_moments

logger = logging.getLogger("gta6_clipper.routes_moments")
router = APIRouter(tags=["moments"])

# In-memory job state & concurrency control
JOBS: Dict[str, Dict] = {}
ACTIVE_JOB_ID: Optional[str] = None
JOB_LOCK = asyncio.Lock()


class EstimateRequest(BaseModel):
    filename: str


class MomentsRequest(BaseModel):
    filename: Optional[str] = None
    url: Optional[str] = None
    streamer_id: Optional[str] = None
    engine: Literal["gemini", "openrouter"] = "gemini"
    max_moments: int = DEFAULT_MAX_MOMENTS
    min_len: float = 15.0
    max_len: float = 60.0
    style_hint: Optional[str] = None


class MomentsExportRequest(BaseModel):
    filename: str
    format: Literal["edl", "csv", "resolve"] = "edl"
    timeline_offset: str = "01:00:00:00"


@router.post("/api/clip/moments/estimate")
async def get_estimate(body: EstimateRequest):
    from gta6_clipper.app import sanitize_clip_path

    file_path = sanitize_clip_path(body.filename)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Clip not found")

    try:
        est = await asyncio.to_thread(estimate_job, file_path)
        return {"status": "ok", "estimate": est}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to estimate moments job: {e}")


@router.post("/api/clip/moments")
async def start_moments_analysis(body: MomentsRequest):
    global ACTIVE_JOB_ID
    from gta6_clipper.app import sanitize_clip_path

    job_id = f"job_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    params = {
        "max_moments": body.max_moments,
        "min_len": body.min_len,
        "max_len": body.max_len,
        "style_hint": body.style_hint.strip() if body.style_hint else None,
    }

    # OpenRouter Engine Flow
    if body.engine == "openrouter":
        openrouter_key = get_openrouter_api_key()
        if not openrouter_key:
            raise HTTPException(
                status_code=400,
                detail="OPENROUTER_API_KEY environment variable is not configured. Set it in your environment or Railway variables.",
            )

        from gta6_clipper.app import (
            DEFAULT_STREAMERS,
            STREAMERS_FILE,
            load_json_safe,
            streamers_lock,
            validate_stream_url,
        )

        target_url = None
        target_name = "stream_highlights"
        if body.url:
            validate_stream_url(body.url)
            target_url = body.url
            target_name = body.url
        elif body.streamer_id:
            streamers = load_json_safe(STREAMERS_FILE, DEFAULT_STREAMERS, streamers_lock)
            if body.streamer_id not in streamers:
                raise HTTPException(status_code=404, detail=f"Streamer '{body.streamer_id}' not found in roster")
            target_url = streamers[body.streamer_id].get("url")
            target_name = streamers[body.streamer_id].get("name", body.streamer_id)
        elif body.filename:
            file_path = sanitize_clip_path(body.filename)
            if not file_path.exists():
                raise HTTPException(status_code=404, detail="Clip not found")
            target_name = body.filename
        else:
            raise HTTPException(status_code=400, detail="Provide a stream URL, streamer_id, or recorded clip")

        async with JOB_LOCK:
            if ACTIVE_JOB_ID and ACTIVE_JOB_ID in JOBS and JOBS[ACTIVE_JOB_ID].get("status") in ("queued", "running"):
                raise HTTPException(
                    status_code=409,
                    detail="Another video analysis job is currently in progress. Please wait for it to complete or cancel it.",
                )
            ACTIVE_JOB_ID = job_id

        JOBS[job_id] = {
            "job_id": job_id,
            "filename": target_name,
            "url": target_url,
            "engine": "openrouter",
            "status": "queued",
            "progress": {"current_chunk": 0, "total_chunks": 1, "percent": 0},
            "results": [],
            "is_cached": False,
            "error": None,
            "created_at": datetime.now().isoformat(),
        }

        asyncio.create_task(_run_openrouter_job(job_id, target_url, body.filename, params, openrouter_key))
        return {"status": "ok", "job_id": job_id, "cached": False}

    # Gemini Engine Flow (Direct Video)
    if not body.filename:
        raise HTTPException(status_code=400, detail="Gemini video engine requires a recorded clip filename")

    file_path = sanitize_clip_path(body.filename)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Clip not found")

    api_key = get_gemini_api_key()
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail="GEMINI_API_KEY environment variable is not configured. Set it in your environment or .env file.",
        )

    cached = load_cached_moments(file_path, get_gemini_model(), params)
    if cached is not None:
        JOBS[job_id] = {
            "job_id": job_id,
            "filename": body.filename,
            "engine": "gemini",
            "status": "done",
            "progress": {"current_chunk": 1, "total_chunks": 1, "percent": 100},
            "results": cached,
            "is_cached": True,
            "error": None,
            "created_at": datetime.now().isoformat(),
        }
        return {"status": "ok", "job_id": job_id, "cached": True}

    async with JOB_LOCK:
        if ACTIVE_JOB_ID and ACTIVE_JOB_ID in JOBS and JOBS[ACTIVE_JOB_ID].get("status") in ("queued", "running"):
            raise HTTPException(
                status_code=409,
                detail="Another video analysis job is currently in progress. Please wait for it to complete or cancel it.",
            )
        ACTIVE_JOB_ID = job_id

    JOBS[job_id] = {
        "job_id": job_id,
        "filename": body.filename,
        "engine": "gemini",
        "status": "queued",
        "progress": {"current_chunk": 0, "total_chunks": 1, "percent": 0},
        "results": [],
        "is_cached": False,
        "error": None,
        "created_at": datetime.now().isoformat(),
    }

    asyncio.create_task(_run_moments_job(job_id, file_path, params, api_key))
    return {"status": "ok", "job_id": job_id, "cached": False}


async def _run_openrouter_job(job_id: str, url: Optional[str], filename: Optional[str], params: Dict, api_key: str):
    """Background processor for OpenRouter highlights (Subtitles + Live Chat Hype Fallback)."""
    global ACTIVE_JOB_ID
    from gta6_clipper.openrouter import (
        fetch_stream_transcript,
        parse_vtt_to_timestamped_text,
        analyse_transcript_openrouter,
        fetch_stream_live_chat,
        fetch_stream_community_highlights,
        parse_live_chat_excitement,
        analyse_chat_spikes_openrouter,
    )

    temp_dir = Path(tempfile.mkdtemp(prefix=f"gta6_openrouter_{job_id}_"))
    job = JOBS[job_id]
    job["status"] = "running"
    job["progress"]["percent"] = 15

    try:
        transcript_text = ""
        moments = []
        detection_source = "captions"

        if not url:
            raise RuntimeError("OpenRouter free highlight extraction requires a YouTube Live or VOD URL.")

        # Step 1: Attempt subtitle/dialogue extraction
        job["progress"]["percent"] = 25
        vtt_path = await asyncio.to_thread(fetch_stream_transcript, url, temp_dir)
        if vtt_path and vtt_path.exists():
            job["progress"]["percent"] = 45
            transcript_text = await asyncio.to_thread(parse_vtt_to_timestamped_text, vtt_path)

        if transcript_text and len(transcript_text.strip()) >= 50:
            # Captions successfully extracted
            job["progress"]["percent"] = 75
            moments = await asyncio.to_thread(
                analyse_transcript_openrouter,
                api_key,
                transcript_text,
                max_moments=params.get("max_moments", 15),
                style_hint=params.get("style_hint"),
            )
            detection_source = "captions"
        else:
            # Step 2: Fallback to Live Chat Replay & Community timestamp analysis
            logger.info(f"Subtitles unavailable or restricted for {url}. Falling back to Live Chat & Community highlights...")
            job["progress"]["percent"] = 40

            # Concurrently fetch community timestamp comments and live chat replay fragments
            community_task = asyncio.to_thread(fetch_stream_community_highlights, url, temp_dir)
            chat_task = asyncio.to_thread(fetch_stream_live_chat, url, temp_dir)
            community_peaks, chat_path = await asyncio.gather(community_task, chat_task)

            chat_peaks = []
            if chat_path and chat_path.exists():
                chat_peaks = await asyncio.to_thread(
                    parse_live_chat_excitement,
                    chat_path,
                    35,
                    params.get("max_moments", 15),
                    180,
                )

            all_candidates = (community_peaks or []) + (chat_peaks or [])
            if not all_candidates:
                raise RuntimeError(
                    "YouTube has restricted auto-captions and live chat replay on this stream (GTA 6 copyright/age policy). "
                    "Tip: You can switch to the 'Gemini 3.8 Flash (Direct Video)' engine to analyze recorded clips visually, "
                    "or provide a YouTube cookies.txt file to unlock restricted streams."
                )

            # Deduplicate and space candidates across the stream (minimum 120s separation)
            all_candidates.sort(key=lambda x: x["start_sec"])
            spaced_candidates = []
            for c in all_candidates:
                if not any(abs(c["start_sec"] - s["start_sec"]) < 120 for s in spaced_candidates):
                    spaced_candidates.append(c)

            job["progress"]["percent"] = 80
            moments = await asyncio.to_thread(
                analyse_chat_spikes_openrouter,
                api_key,
                spaced_candidates,
                max_moments=params.get("max_moments", 15),
                style_hint=params.get("style_hint"),
            )
            detection_source = "live_chat"

        job["progress"]["percent"] = 100
        job["status"] = "done"
        job["detection_source"] = detection_source
        job["results"] = moments

    except Exception as e:
        logger.error(f"OpenRouter job {job_id} error: {e}")
        safe_msg = str(e)
        if "api_key" in safe_msg.lower() or "bearer" in safe_msg.lower() or "401" in safe_msg:
            safe_msg = "OpenRouter API key error. Please check your OPENROUTER_API_KEY."
        job["status"] = "error"
        job["error"] = safe_msg

    finally:
        async with JOB_LOCK:
            if ACTIVE_JOB_ID == job_id:
                ACTIVE_JOB_ID = None
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


async def _run_moments_job(job_id: str, file_path: Path, params: Dict, api_key: str):
    """Background processor for video highlight extraction."""
    global ACTIVE_JOB_ID
    from google import genai

    temp_dir = Path(tempfile.mkdtemp(prefix=f"gta6_moments_{job_id}_"))
    job = JOBS[job_id]
    job["status"] = "running"

    try:
        source_dur = get_media_duration(file_path)
        from google.genai import types
        client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=60000))

        # Step 1: Generate lightweight proxy
        job["progress"]["percent"] = 5
        proxy_path = await asyncio.to_thread(make_proxy, file_path, temp_dir)

        # Step 2: Split into chunks
        chunk_min = float(os.getenv("CHUNK_MINUTES", str(DEFAULT_CHUNK_MINUTES)))
        chunks = await asyncio.to_thread(split_chunks, proxy_path, chunk_min, 30.0, temp_dir)

        total_chunks = len(chunks)
        job["progress"]["total_chunks"] = total_chunks
        job["progress"]["percent"] = 15

        all_raw_moments = []
        delay_sec = float(os.getenv("MOMENTS_DELAY_SECONDS", str(DEFAULT_DELAY_SECONDS)))

        # Step 3: Analyze each chunk
        for idx, (chunk_file, offset_sec) in enumerate(chunks):
            # Check cancellation
            if job.get("status") == "cancelled":
                logger.info(f"Moments job {job_id} cancelled by user")
                return

            job["progress"]["current_chunk"] = idx + 1
            progress_step = 15 + int(((idx) / total_chunks) * 75)
            job["progress"]["percent"] = min(90, progress_step)

            # Analyze single chunk
            raw_chunk_moments = await asyncio.to_thread(
                analyse_chunk_gemini,
                client,
                chunk_file,
                style_hint=params.get("style_hint"),
                thinking_level="low",
            )

            # Apply offset correction & clamp to duration
            for m in raw_chunk_moments:
                try:
                    s_sec = parse_time_offset(m.get("start", 0)) + offset_sec
                    e_sec = parse_time_offset(m.get("end", 0)) + offset_sec
                    s_sec = max(0.0, min(s_sec, source_dur))
                    e_sec = max(s_sec + 1.0, min(e_sec, source_dur))

                    all_raw_moments.append({
                        "start_sec": s_sec,
                        "end_sec": e_sec,
                        "start_hms": format_seconds_to_hms(s_sec),
                        "end_hms": format_seconds_to_hms(e_sec),
                        "score": int(m.get("score", 5)),
                        "title": m.get("title", "Highlight"),
                        "reason": m.get("reason", ""),
                    })
                except Exception as parse_err:
                    logger.warning(f"Error parsing moment item: {parse_err}")

            # Free-tier rate limit delay between chunks
            if idx < (total_chunks - 1):
                await asyncio.sleep(delay_sec)

        # Step 4: Merge moments across chunks
        final_moments = merge_moments(
            all_raw_moments,
            min_len=params.get("min_len", 15.0),
            max_len=params.get("max_len", 60.0),
            max_moments=params.get("max_moments", DEFAULT_MAX_MOMENTS),
        )

        # Step 5: Save to sidecar cache
        save_cached_moments(file_path, get_gemini_model(), params, final_moments)

        job["status"] = "done"
        job["progress"]["percent"] = 100
        job["progress"]["current_chunk"] = total_chunks
        job["results"] = final_moments

    except Exception as e:
        logger.error(f"Moments job {job_id} error: {e}")
        # Safe error message (Never leak secrets or system paths)
        safe_msg = str(e)
        if "api_key" in safe_msg.lower() or "gemini" in safe_msg.lower():
            safe_msg = "Gemini API error or invalid API key. Please check your quota and key."
        job["status"] = "error"
        job["error"] = safe_msg

    finally:
        async with JOB_LOCK:
            if ACTIVE_JOB_ID == job_id:
                ACTIVE_JOB_ID = None
        # Clean up temporary proxy and chunks
        if temp_dir.exists():
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass


@router.get("/api/clip/moments/{job_id}")
async def get_moments_job(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail="Job ID not found")
    job = JOBS[job_id]
    return {
        "status": job["status"],
        "job_id": job["job_id"],
        "filename": job["filename"],
        "progress": job["progress"],
        "results": job["results"],
        "error": job["error"],
        "is_cached": job.get("is_cached", False),
    }


@router.post("/api/clip/moments/{job_id}/cancel")
async def cancel_moments_job(job_id: str):
    global ACTIVE_JOB_ID
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail="Job ID not found")

    job = JOBS[job_id]
    if job["status"] in ("queued", "running"):
        job["status"] = "cancelled"
        job["error"] = "Cancelled by user"

    async with JOB_LOCK:
        if ACTIVE_JOB_ID == job_id:
            ACTIVE_JOB_ID = None

    return {"status": "ok", "cancelled": job_id}


@router.post("/api/clip/moments/export")
async def export_clip_moments(body: MomentsExportRequest):
    from gta6_clipper.app import sanitize_clip_path

    file_path = sanitize_clip_path(body.filename)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Clip not found")

    # Read moments from sidecar cache
    params = {
        "max_moments": DEFAULT_MAX_MOMENTS,
        "min_len": 15.0,
        "max_len": 60.0,
        "style_hint": None,
    }
    moments = load_cached_moments(file_path, get_gemini_model(), params)
    if not moments:
        # Check any completed job
        matched = next((j["results"] for j in JOBS.values() if j.get("filename") == body.filename and j.get("status") == "done"), None)
        moments = matched or []

    if not moments:
        # Fallback: check if sidecar file exists with any params
        sidecar = file_path.with_suffix(file_path.suffix + ".moments.json")
        if sidecar.exists():
            try:
                import json
                s_data = json.loads(sidecar.read_text(encoding="utf-8"))
                moments = s_data.get("moments", [])
            except Exception:
                pass

    if not moments:
        raise HTTPException(
            status_code=400,
            detail="No analyzed moments found for this clip. Run 'Find Best Moments' first.",
        )

    try:
        res = export_moments(
            file_path,
            moments,
            fmt=body.format,
            timeline_offset=body.timeline_offset,
        )
        return res
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Export failed: {e}")
