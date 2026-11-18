# GTA6-ZeroHour // Live Stream Clipper

High-performance DVR live-stream recording and zero-re-encode clipping system for GTA 6 launch day streaming events.

## Usage Notice
> **Notice**: This tool is intended for capturing and clipping live streams and video content you own or have explicit permission to record, clip, and edit. Always respect streamers' content rights and platform terms of service.

## Features
- **Zero Re-Encode Rolling Buffer**: Records streams into 6-second MPEG-TS chunks; cuts sub-clips in ~0.2s via `-c copy`.
- **Live Stream Timestamp Extractor (`POST /api/clip/extract`)**: Extract exact sections (e.g. `00:15:00` to `00:25:00`) from active YouTube live streams (via DVR rewind `--live-from-start`) or VODs without waiting for multi-hour streams to finish.
- **Interactive Video Clip Trimmer (`POST /api/clip/trim`)**: Set In/Out markers on any generated clip directly in the browser and slice sub-clips in Fast mode (`-c copy`) or Exact mode (libx264/AAC re-encode).
- **Multi-Platform Support**: YouTube, Twitch, Kick.
- **Dark Mode Mission-Control Dashboard**: Live buffer health, storage monitoring, 10m/20m quick-clips, and instant preview.

## Running Locally
```bash
./gta6_clipper/.venv/bin/uvicorn gta6_clipper.app:app --host 127.0.0.1 --port 8000
```
Open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your browser.
