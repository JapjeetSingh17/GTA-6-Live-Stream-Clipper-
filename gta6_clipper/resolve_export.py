import csv
import io
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("gta6_clipper.resolve_export")

def sec_to_tc(seconds: float, fps: float = 30.0) -> str:
    """Convert seconds to standard SMPTE timecode (HH:MM:SS:FF)."""
    frames_total = int(round(seconds * fps))
    fps_int = max(1, int(round(fps)))
    ff = frames_total % fps_int
    total_sec = frames_total // fps_int
    ss = total_sec % 60
    mm = (total_sec // 60) % 60
    hh = total_sec // 3600
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"

def tc_to_frames(tc: str, fps: float = 30.0) -> int:
    """Convert SMPTE timecode (HH:MM:SS:FF or HH:MM:SS) to total frames."""
    parts = tc.strip().split(":")
    fps_int = max(1, int(round(fps)))
    if len(parts) == 4:
        h, m, s, f = map(int, parts)
        return (h * 3600 + m * 60 + s) * fps_int + f
    elif len(parts) == 3:
        h, m, s = map(int, parts)
        return (h * 3600 + m * 60 + s) * fps_int
    return 0

def frames_to_tc(frames: int, fps: float = 30.0) -> str:
    """Convert frame count back to SMPTE timecode."""
    fps_int = max(1, int(round(fps)))
    ff = frames % fps_int
    total_sec = frames // fps_int
    ss = total_sec % 60
    mm = (total_sec // 60) % 60
    hh = total_sec // 3600
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"

def generate_edl(
    filename: str,
    moments: List[Dict],
    fps: float = 30.0,
    timeline_offset: str = "01:00:00:00",
) -> str:
    """
    Generate CMX 3600 Edit Decision List (EDL) with colored timeline markers.
    Color: Score >= 8 -> GREEN, Score < 8 -> YELLOW.
    Name = title, Note = reason.
    """
    lines = [
        f"TITLE: GTA6 AI Highlights - {filename}",
        "FCM: NON-DROP FRAME",
        "",
    ]

    offset_frames = tc_to_frames(timeline_offset, fps)
    record_frames = offset_frames

    for idx, m in enumerate(moments, start=1):
        s_sec = float(m.get("start_sec", 0.0))
        e_sec = float(m.get("end_sec", s_sec + 15.0))
        dur_frames = max(1, int(round((e_sec - s_sec) * fps)))

        src_in = sec_to_tc(s_sec, fps)
        src_out = sec_to_tc(e_sec, fps)
        rec_in = frames_to_tc(record_frames, fps)
        rec_out = frames_to_tc(record_frames + dur_frames, fps)
        record_frames += dur_frames

        score = int(m.get("score", 5))
        color = "GREEN" if score >= 8 else "YELLOW"
        title = str(m.get("title", f"Moment {idx}")).replace("\n", " ").strip()
        reason = str(m.get("reason", "")).replace("\n", " ").strip()

        # CMX 3600 event line
        lines.append(f"{idx:03d}  AX       V     C        {src_in} {src_out} {rec_in} {rec_out}")
        lines.append(f"* FROM CLIP NAME: {filename}")
        lines.append(f"* MARKER: {color} {title} | {reason} (Score: {score})")
        lines.append("")

    return "\n".join(lines)

def generate_csv(moments: List[Dict]) -> str:
    """Generate CSV of moments for DaVinci Resolve / Premiere marker import."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([
        "Index", "Start", "End", "StartSec", "EndSec", "DurationSec",
        "Score", "Color", "Title", "Reason"
    ])
    for idx, m in enumerate(moments, start=1):
        s_sec = float(m.get("start_sec", 0.0))
        e_sec = float(m.get("end_sec", s_sec + 15.0))
        score = int(m.get("score", 5))
        color = "Green" if score >= 8 else "Yellow"
        writer.writerow([
            idx,
            m.get("start", sec_to_tc(s_sec)[:8]),
            m.get("end", sec_to_tc(e_sec)[:8]),
            round(s_sec, 2),
            round(e_sec, 2),
            round(e_sec - s_sec, 2),
            score,
            color,
            m.get("title", f"Moment {idx}"),
            m.get("reason", ""),
        ])
    return out.getvalue()

def try_davinci_resolve_scripting(
    clip_path: Path,
    moments: List[Dict],
    fps: float = 30.0,
) -> Tuple[bool, str]:
    """
    Optional: Attempt to connect to DaVinci Resolve Studio via Python API.
    If unavailable, returns (False, error_reason).
    Never crashes if Resolve Studio is missing.
    """
    try:
        import sys
        # Standard Blackmagic Design DaVinci Resolve Studio module paths on macOS/Linux/Windows
        candidates = [
            "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules",
            "/opt/resolve/Developer/Scripting/Modules",
            "C:\\ProgramData\\Blackmagic Design\\DaVinci Resolve\\Support\\Developer\\Scripting\\Modules",
        ]
        for c in candidates:
            if Path(c).exists() and c not in sys.path:
                sys.path.append(c)

        import DaVinciResolveScript as bmd  # type: ignore
        resolve = bmd.scriptapp("Resolve")
        if not resolve:
            return False, "DaVinci Resolve Studio application is not running."

        project_manager = resolve.GetProjectManager()
        project = project_manager.GetCurrentProject()
        if not project:
            return False, "No active project in DaVinci Resolve."

        media_pool = project.GetMediaPool()
        # Add source clip to media pool
        media_items = media_pool.ImportMedia([str(clip_path.resolve())])
        if not media_items:
            return False, "Failed to import clip into DaVinci Resolve Media Pool."

        target_clip = media_items[0]
        root_folder = media_pool.GetRootFolder()

        # Create Timeline with highlights
        timeline_name = f"GTA6 Highlights - {clip_path.stem}"
        timeline = media_pool.CreateEmptyTimeline(timeline_name)
        if not timeline:
            return False, "Failed to create timeline in DaVinci Resolve."

        # Add markers to timeline for each moment
        for m in moments:
            s_sec = float(m.get("start_sec", 0.0))
            frame_id = int(round(s_sec * fps))
            score = int(m.get("score", 5))
            color = "Green" if score >= 8 else "Yellow"
            title = str(m.get("title", "Highlight"))
            note = str(m.get("reason", ""))
            timeline.AddMarker(frame_id, color, title, note, 1)

        return True, f"Successfully created timeline '{timeline_name}' with {len(moments)} markers in DaVinci Resolve Studio."

    except ImportError:
        return False, "DaVinci Resolve Scripting module not found (Requires DaVinci Resolve Studio)."
    except Exception as e:
        return False, f"DaVinci Resolve integration error: {e}"

def export_moments(
    clip_path: Path,
    moments: List[Dict],
    fmt: str = "edl",
    timeline_offset: str = "01:00:00:00",
    output_dir: Optional[Path] = None,
) -> Dict:
    """
    Export moments to EDL, CSV, or attempt Resolve Studio scripting with clean fallback.
    """
    if output_dir is None:
        output_dir = clip_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    base_stem = clip_path.stem
    fps = 30.0

    if fmt == "edl":
        edl_text = generate_edl(clip_path.name, moments, fps=fps, timeline_offset=timeline_offset)
        out_file = output_dir / f"{base_stem}_highlights.edl"
        out_file.write_text(edl_text, encoding="utf-8")
        return {
            "status": "ok",
            "format": "edl",
            "filename": out_file.name,
            "url": f"/clips/{out_file.name}",
            "message": "EDL file generated successfully for DaVinci Resolve / Premiere marker import.",
        }

    elif fmt == "csv":
        csv_text = generate_csv(moments)
        out_file = output_dir / f"{base_stem}_highlights.csv"
        out_file.write_text(csv_text, encoding="utf-8")
        return {
            "status": "ok",
            "format": "csv",
            "filename": out_file.name,
            "url": f"/clips/{out_file.name}",
            "message": "CSV marker file generated successfully.",
        }

    elif fmt == "resolve":
        success, msg = try_davinci_resolve_scripting(clip_path, moments, fps=fps)
        if success:
            return {
                "status": "ok",
                "format": "resolve_direct",
                "message": msg,
            }
        else:
            # Clean fallback to EDL & CSV
            edl_text = generate_edl(clip_path.name, moments, fps=fps, timeline_offset=timeline_offset)
            edl_file = output_dir / f"{base_stem}_highlights.edl"
            edl_file.write_text(edl_text, encoding="utf-8")

            csv_text = generate_csv(moments)
            csv_file = output_dir / f"{base_stem}_highlights.csv"
            csv_file.write_text(csv_text, encoding="utf-8")

            return {
                "status": "ok",
                "format": "resolve_fallback_edl",
                "message": f"{msg} Exported EDL and CSV files instead.",
                "edl_filename": edl_file.name,
                "edl_url": f"/clips/{edl_file.name}",
                "csv_filename": csv_file.name,
                "csv_url": f"/clips/{csv_file.name}",
            }
    else:
        raise ValueError(f"Unsupported export format '{fmt}'. Choose 'edl', 'csv', or 'resolve'.")
