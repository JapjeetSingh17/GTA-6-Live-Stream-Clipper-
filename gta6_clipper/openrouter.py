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

HYPE_PATTERN = re.compile(
    r"\b(omg|wtf|lmao|lol|pog|poggers|kekw|holy|sh[i*]t|crazy|nooo+|rip|dead|w\b|l\b|clutch|hacker|insane|stunt|police|busted|aimbot|bussin|fire|wth|damn|no way|sheesh|clip)\b|[💀🔥😂😱🤣💥🚨]",
    re.IGNORECASE,
)


def get_cookie_args(temp_dir: Optional[Path] = None) -> List[str]:
    """
    Return yt-dlp arguments for authentication if cookies are available.
    Supports local cookies.txt in workspace root or YOUTUBE_COOKIES environment variable.
    """
    args = ["--force-ipv4"]
    cwd_cookie = Path.cwd() / "cookies.txt"
    if cwd_cookie.exists():
        args.extend(["--cookies", str(cwd_cookie)])
        return args

    env_cookies = os.getenv("YOUTUBE_COOKIES", "").strip()
    if env_cookies and temp_dir:
        c_path = temp_dir / "yt_cookies.txt"
        try:
            c_path.write_text(env_cookies, encoding="utf-8")
            args.extend(["--cookies", str(c_path)])
            return args
        except Exception:
            pass

    args.extend(["--extractor-args", "youtube:player_client=ios,android,web"])
    return args


def get_openrouter_api_key() -> Optional[str]:
    """Retrieve OPENROUTER_API_KEY from environment."""
    return os.getenv("OPENROUTER_API_KEY", "").strip() or None


def get_openrouter_model() -> str:
    """Retrieve configured OpenRouter model, defaulting to free tier."""
    return os.getenv("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL).strip() or DEFAULT_OPENROUTER_MODEL


def fetch_stream_transcript(url: str, temp_dir: Path) -> Optional[Path]:
    """
    Extract auto-generated or official timestamped subtitles using yt-dlp without downloading video.
    Returns path to downloaded .vtt/.srt file, or None if no captions available.
    """
    yt_dlp_bin = shutil.which("yt-dlp")
    if not yt_dlp_bin:
        raise RuntimeError("yt-dlp is not installed on this system")

    cookie_args = get_cookie_args(temp_dir)
    output_tmpl = str(temp_dir / "subs")
    cmd = [
        yt_dlp_bin,
        "--no-warnings",
        "--skip-download",
        "--write-auto-subs",
        "--write-subs",
        "--sub-langs", "all,-live_chat",
        "--sub-format", "vtt/srt/best",
        *cookie_args,
        "-o", output_tmpl,
        "--",
        url,
    ]

    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
        if proc.returncode != 0 and proc.stderr:
            logger.info(f"yt-dlp subtitle info: {proc.stderr[:200]}")
    except subprocess.TimeoutExpired:
        raise TimeoutError("Timed out extracting stream subtitles")
    except Exception as e:
        logger.warning(f"Error executing yt-dlp subtitle extraction: {e}")

    candidates = list(temp_dir.glob("*.vtt")) + list(temp_dir.glob("*.srt"))
    if candidates:
        return candidates[0]

    return None


def fetch_stream_live_chat(url: str, temp_dir: Path, timeout_sec: int = 45) -> Optional[Path]:
    """
    Extract YouTube live chat replay JSON using yt-dlp without downloading video.
    Returns path to downloaded .live_chat.json or .part file, or None if no chat replay is available.
    """
    yt_dlp_bin = shutil.which("yt-dlp")
    if not yt_dlp_bin:
        raise RuntimeError("yt-dlp is not installed on this system")

    cookie_args = get_cookie_args(temp_dir)
    output_tmpl = str(temp_dir / "%(id)s")
    cmd = [
        yt_dlp_bin,
        "--no-warnings",
        "--skip-download",
        "--write-subs",
        "--sub-langs", "live_chat",
        *cookie_args,
        "-o", output_tmpl,
        "--",
        url,
    ]

    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout_sec)
        if proc.returncode != 0 and proc.stderr:
            logger.warning(f"yt-dlp live chat returned {proc.returncode}: {proc.stderr[:300]}")
    except subprocess.TimeoutExpired:
        logger.info(f"Live chat extraction sample completed after {timeout_sec}s")
    except Exception as e:
        logger.warning(f"Live chat extraction error: {e}")

    chat_candidates = list(temp_dir.glob("*live_chat*"))
    if chat_candidates:
        chat_candidates.sort(key=lambda p: p.stat().st_size, reverse=True)
        return chat_candidates[0]

    return None


def parse_live_chat_excitement(
    chat_path: Path,
    window_sec: int = 35,
    top_k: int = 15,
) -> List[Dict]:
    """
    Parse YouTube .live_chat.json files, aggregate viewer chat velocity & hype words
    into time windows, and return the top excitement peak moments with sample chat reactions.
    """
    if not chat_path.exists():
        return []

    buckets: Dict[int, Dict] = {}

    # Read the main chat file or any downloaded fragments in the directory
    files_to_read = [chat_path]
    if chat_path.parent.exists():
        all_chats = list(chat_path.parent.glob("*live_chat*"))
        if all_chats:
            files_to_read = all_chats

    for fpath in files_to_read:
        try:
            with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line_str = line.strip()
                    if not line_str:
                        continue
                    try:
                        data = json.loads(line_str)
                    except Exception:
                        continue

                    action = data.get("replayChatItemAction", {})
                    offset_str = action.get("videoOffsetTimeMsec", "0")
                    try:
                        offset_msec = int(offset_str)
                    except (ValueError, TypeError):
                        continue

                    sec = max(0, offset_msec // 1000)
                    b_idx = sec // window_sec

                    text_runs = []
                    for act in action.get("actions", []):
                        for key in ("addChatItemAction", "addLiveChatTickerItemAction"):
                            item = act.get(key, {}).get("item", {})
                            msg_renderer = (
                                item.get("liveChatTextMessageRenderer")
                                or item.get("liveChatPaidMessageRenderer")
                                or item.get("liveChatMembershipItemRenderer")
                            )
                            if msg_renderer:
                                runs = msg_renderer.get("message", {}).get("runs", [])
                                text = "".join(r.get("text", "") for r in runs if isinstance(r, dict))
                                author = msg_renderer.get("authorName", {}).get("simpleText", "viewer")
                                if text:
                                    text_runs.append((author, text.strip()))

                    if not text_runs:
                        continue

                    if b_idx not in buckets:
                        buckets[b_idx] = {"msgs": 0, "hype": 0, "samples": []}

                    for author, text in text_runs:
                        buckets[b_idx]["msgs"] += 1
                        hype_matches = len(HYPE_PATTERN.findall(text))
                        buckets[b_idx]["hype"] += hype_matches
                        if len(buckets[b_idx]["samples"]) < 6 and len(text) > 2:
                            buckets[b_idx]["samples"].append(f"@{author}: {text}")

        except Exception as e:
            logger.warning(f"Error reading live chat file {fpath.name}: {e}")

    if not buckets:
        return []

    scored_windows = []
    for b_idx, stats in buckets.items():
        if stats["msgs"] < 2:
            continue
        raw_score = stats["msgs"] * 1.0 + stats["hype"] * 2.5
        s_sec = b_idx * window_sec
        e_sec = s_sec + window_sec
        scored_windows.append({
            "bucket": b_idx,
            "start_sec": s_sec,
            "end_sec": e_sec,
            "score": raw_score,
            "msgs": stats["msgs"],
            "hype": stats["hype"],
            "samples": stats["samples"],
        })

    scored_windows.sort(key=lambda x: x["score"], reverse=True)

    selected_peaks = []
    for win in scored_windows:
        overlap = any(abs(win["start_sec"] - p["start_sec"]) < window_sec for p in selected_peaks)
        if not overlap:
            selected_peaks.append(win)
            if len(selected_peaks) >= top_k:
                break

    results = []
    for p in selected_peaks:
        results.append({
            "start": format_seconds_to_hms(p["start_sec"]),
            "end": format_seconds_to_hms(p["end_sec"]),
            "start_sec": p["start_sec"],
            "end_sec": p["end_sec"],
            "duration_sec": p["end_sec"] - p["start_sec"],
            "raw_score": p["score"],
            "msgs": p["msgs"],
            "hype": p["hype"],
            "samples": p["samples"],
        })

    results.sort(key=lambda x: x["start_sec"])
    return results


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
                "source": "captions",
            })
        except Exception:
            continue

    # Sort by score descending and take top max_moments
    valid_moments.sort(key=lambda x: x["score"], reverse=True)
    top_moments = valid_moments[:max_moments]
    # Re-sort chronologically for timeline editing
    top_moments.sort(key=lambda x: x["start_sec"])
    return top_moments


def analyse_chat_spikes_openrouter(
    api_key: str,
    peaks: List[Dict],
    max_moments: int = 15,
    style_hint: Optional[str] = None,
    model: Optional[str] = None,
) -> List[Dict]:
    """
    Given detected excitement peaks from YouTube live chat replay, query OpenRouter free LLM
    to generate punchy viral titles, viral score (1-10), and descriptions of the stream event.
    """
    if not peaks:
        return []

    system_instruction = (
        "You are an elite gaming stream editor and viral video curator specializing in Grand Theft Auto (GTA). "
        "You are given candidate highlight timestamps extracted from massive viewer live chat excitement explosions. "
        "For each peak timestamp window, analyze the viewer chat reactions (shouting, memes, laughter, shock) "
        "and determine what in-game event caused the reaction. "
        "Generate a punchy, click-worthy viral title, a viral score from 1 to 10, and a 1-sentence reason. "
        "You MUST output valid JSON matching this schema:\n"
        "{\n"
        '  "moments": [\n'
        '    {\n'
        '      "start": "HH:MM:SS",\n'
        '      "end": "HH:MM:SS",\n'
        '      "score": 1 to 10,\n'
        '      "title": "Punchy viral clip title",\n'
        '      "reason": "1-sentence description of the moment from chat reactions"\n'
        '    }\n'
        '  ]\n'
        "}"
    )

    prompt_lines = ["PEAK LIVE CHAT EXCITEMENT WINDOWS:\n"]
    if style_hint and style_hint.strip():
        prompt_lines.append(f"STYLE PREFERENCE: {style_hint.strip()}\n")

    for i, p in enumerate(peaks):
        chat_summary = " | ".join(p.get("samples", [])) or "High volume chat reaction"
        prompt_lines.append(
            f"Peak #{i+1}: [{p['start']} - {p['end']}] (Chat messages: {p['msgs']}, Hype score: {int(p['raw_score'])})\n"
            f"Viewer Reactions: {chat_summary}\n"
        )

    user_prompt = "\n".join(prompt_lines)

    raw_response = call_openrouter_chat(
        api_key=api_key,
        prompt=user_prompt,
        system_instruction=system_instruction,
        model=model,
    )

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
        logger.warning(f"Failed to parse LLM chat moments JSON: {err}, falling back to peak metrics")
        return [
            {
                "start": p["start"],
                "end": p["end"],
                "start_sec": p["start_sec"],
                "end_sec": p["end_sec"],
                "duration_sec": p["duration_sec"],
                "score": 8,
                "title": f"Chat Hype Moment at {p['start']}",
                "reason": f"Explosion of {p['msgs']} live chat messages and viewer hype",
                "source": "live_chat",
            }
            for p in peaks[:max_moments]
        ]

    raw_moments = parsed.get("moments", [])
    valid_moments = []

    for idx, m in enumerate(raw_moments):
        try:
            s_str = m.get("start", "")
            e_str = m.get("end", "")
            if not s_str or not e_str:
                if idx < len(peaks):
                    s_str = peaks[idx]["start"]
                    e_str = peaks[idx]["end"]
                else:
                    continue

            s_sec = parse_time_offset(s_str)
            e_sec = parse_time_offset(e_str)
            if e_sec <= s_sec:
                e_sec = s_sec + 35.0

            valid_moments.append({
                "start": format_seconds_to_hms(s_sec),
                "end": format_seconds_to_hms(e_sec),
                "start_sec": s_sec,
                "end_sec": e_sec,
                "duration_sec": round(e_sec - s_sec, 1),
                "score": int(m.get("score", 8)),
                "title": m.get("title", f"GTA 6 Chat Peak #{idx+1}"),
                "reason": m.get("reason", "Intense viewer reaction spike"),
                "source": "live_chat",
            })
        except Exception:
            continue

    if not valid_moments and peaks:
        for idx, p in enumerate(peaks[:max_moments]):
            valid_moments.append({
                "start": p["start"],
                "end": p["end"],
                "start_sec": p["start_sec"],
                "end_sec": p["end_sec"],
                "duration_sec": p["duration_sec"],
                "score": 8,
                "title": f"Live Chat Reaction at {p['start']}",
                "reason": f"Spike of {p['msgs']} chat messages",
                "source": "live_chat",
            })

    valid_moments.sort(key=lambda x: x["start_sec"])
    return valid_moments[:max_moments]
