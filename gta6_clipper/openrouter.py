"""
OpenRouter Free LLM Integration for Long-Stream Highlights.
Extracts timestamped captions from long streams (6-8 hours) in seconds without downloading video,
and queries OpenRouter free models (:free) to identify viral moments at $0.00 cost.
"""

from datetime import datetime
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Dict, List, Optional
import urllib.request
import urllib.error

from gta6_clipper.extractor import format_seconds_to_hms, parse_time_offset

logger = logging.getLogger("gta6_clipper.openrouter")

OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_OPENROUTER_MODEL = "google/gemini-2.0-flash-exp:free"


def get_openrouter_api_key() -> Optional[str]:
    """Retrieve OPENROUTER_API_KEY from environment."""
    return os.getenv("OPENROUTER_API_KEY", "").strip() or None


def get_openrouter_model() -> str:
    """Retrieve configured OpenRouter model, defaulting to free tier."""
    return os.getenv("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL).strip() or DEFAULT_OPENROUTER_MODEL


def fetch_stream_transcript(url: str, temp_dir: Path) -> Optional[Path]:
    """
    Extract auto-generated or official timestamped subtitles using yt-dlp without downloading video.
    Returns path to downloaded .vtt file, or None if no captions available.
    """
    yt_dlp_bin = shutil.which("yt-dlp")
    if not yt_dlp_bin:
        raise RuntimeError("yt-dlp is not installed on this system")

    output_tmpl = str(temp_dir / "subs")
    cmd = [
        yt_dlp_bin,
        "--no-warnings",
        "--skip-download",
        "--write-auto-subs",
        "--write-subs",
        "--sub-lang", "en.*,en,en-orig",
        "--sub-format", "vtt",
        "-o", output_tmpl,
        "--",
        url,
    ]

    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise TimeoutError("Timed out extracting stream subtitles")

    # Locate generated .vtt file
    vtt_candidates = list(temp_dir.glob("*.vtt"))
    if vtt_candidates:
        return vtt_candidates[0]

    return None


def parse_vtt_to_timestamped_text(vtt_path: Path, max_lines: int = 15000) -> str:
    """
    Convert a WebVTT caption file into compact timestamped dialogue text:
    [HH:MM:SS] dialog text...
    Deduplicates repetitive YouTube rolling auto-caption lines.
    """
    if not vtt_path.exists():
        return ""

    raw_text = vtt_path.read_text(encoding="utf-8", errors="ignore")
    lines = raw_text.splitlines()

    timestamp_regex = re.compile(r"^(\d{2}:\d{2}:\d{2})\.\d+\s*-->\s*(\d{2}:\d{2}:\d{2})\.\d+")
    tag_cleaner = re.compile(r"<[^>]+>")

    compact_entries = []
    last_text = ""
    current_ts = None

    for line in lines:
        line_clean = line.strip()
        if not line_clean:
            continue

        ts_match = timestamp_regex.match(line_clean)
        if ts_match:
            current_ts = ts_match.group(1)
            continue

        # Skip WebVTT headers / metadata
        if line_clean.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")):
            continue

        if current_ts:
            clean_dialog = tag_cleaner.sub("", line_clean).strip()
            # Clean repetitive speech artifacts
            if clean_dialog and clean_dialog != last_text and len(clean_dialog) > 1:
                compact_entries.append(f"[{current_ts}] {clean_dialog}")
                last_text = clean_dialog

            if len(compact_entries) >= max_lines:
                break

    return "\n".join(compact_entries)


def call_openrouter_chat(
    api_key: str,
    prompt: str,
    system_instruction: str,
    model: Optional[str] = None,
    timeout_sec: int = 60,
) -> str:
    """
    Send standard OpenAI-compatible Chat Completion request to OpenRouter.
    """
    target_model = model or get_openrouter_model()

    payload = {
        "model": target_model,
        "messages": [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
    }

    req_data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        OPENROUTER_ENDPOINT,
        data=req_data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://gta6-live-stream-clipper-production.up.railway.app",
            "X-Title": "GTA6-ZeroHour Stream Clipper",
            "Content-Type": "application/json",
            "User-Agent": "GTA6-ZeroHour/2.0",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as response:
            resp_body = response.read().decode("utf-8")
            data = json.loads(resp_body)
            choices = data.get("choices", [])
            if choices and "message" in choices[0]:
                return choices[0]["message"].get("content", "")
            raise ValueError(f"OpenRouter response missing choices: {resp_body[:200]}")

    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"OpenRouter API error (HTTP {e.code}): {error_body[:250]}")
    except Exception as e:
        raise RuntimeError(f"OpenRouter connection failed: {e}")


def analyse_transcript_openrouter(
    api_key: str,
    transcript_text: str,
    max_moments: int = 15,
    style_hint: Optional[str] = None,
    model: Optional[str] = None,
) -> List[Dict]:
    """
    Analyze stream transcript using OpenRouter free LLM and return structured moments.
    """
    if not transcript_text or len(transcript_text.strip()) < 50:
        raise ValueError("Transcript is too short or empty to extract highlights.")

    system_instruction = (
        "You are an expert streaming video editor and viral clip curator. "
        "Your task is to analyze timestamped stream dialogue/captions and identify the absolute best moments "
        f"for short viral clips (15 to 60 seconds each). Extract up to {max_moments} distinct moments. "
        "Prioritize: high-energy reactions, hilarious banter, intense gameplay events, unexpected twists, and emotional peaks. "
        "Ensure start and end timestamps are in HH:MM:SS format and do not overlap. "
        "You MUST output valid JSON matching this schema:\n"
        "{\n"
        '  "moments": [\n'
        '    {\n'
        '      "start": "HH:MM:SS",\n'
        '      "end": "HH:MM:SS",\n'
        '      "score": 1 to 10,\n'
        '      "title": "Punchy viral clip title",\n'
        '      "reason": "1-sentence reason why this moment is a highlight"\n'
        '    }\n'
        '  ]\n'
        "}"
    )

    user_prompt = "STREAM TRANSCRIPT:\n\n"
    if style_hint and style_hint.strip():
        user_prompt += f"STYLE PREFERENCE: {style_hint.strip()}\n\n"
    user_prompt += transcript_text

    raw_response = call_openrouter_chat(
        api_key=api_key,
        prompt=user_prompt,
        system_instruction=system_instruction,
        model=model,
    )

    # Clean markdown json tags if wrapped
    cleaned = raw_response.strip()
    if cleaned.startswith("```"):
        parts = cleaned.split("```")
        if len(parts) >= 2:
            cleaned = parts[1]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:].strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as err:
        raise ValueError(f"Failed to parse OpenRouter JSON output: {err}")

    raw_moments = parsed.get("moments", [])
    valid_moments = []

    for idx, m in enumerate(raw_moments):
        try:
            s_str = m.get("start", "00:00:00")
            e_str = m.get("end", "00:00:30")
            s_sec = parse_time_offset(s_str)
            e_sec = parse_time_offset(e_str)

            # Enforce sensible duration bounds (10s to 120s)
            if e_sec <= s_sec:
                e_sec = s_sec + 30.0
            dur_sec = e_sec - s_sec
            if dur_sec < 10.0:
                e_sec = s_sec + 20.0
            elif dur_sec > 180.0:
                e_sec = s_sec + 60.0

            valid_moments.append({
                "start": format_seconds_to_hms(s_sec),
                "end": format_seconds_to_hms(e_sec),
                "start_sec": s_sec,
                "end_sec": e_sec,
                "duration_sec": round(e_sec - s_sec, 1),
                "score": int(m.get("score", 7)),
                "title": m.get("title", f"Stream Highlight #{idx+1}"),
                "reason": m.get("reason", "High-energy stream event"),
            })
        except Exception:
            continue

    # Sort by score descending and take top max_moments
    valid_moments.sort(key=lambda x: x["score"], reverse=True)
    top_moments = valid_moments[:max_moments]
    # Re-sort chronologically for timeline editing
    top_moments.sort(key=lambda x: x["start_sec"])
    return top_moments
