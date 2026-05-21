"""
routes/ai_tools/find_dvr_segment.py — implementation + schema for the `find_dvr_segment` tool.

Extracted from the legacy monolithic ai_tools.py (Phase J modularization).
The function and schema live together so adding/changing a tool is a single-
file change. ``__init__.py`` aggregates SCHEMA from every tool module into the
``TOOLS`` list that the chat endpoint passes to Ollama.
"""

import json
import logging
import os
from datetime import datetime, timedelta


from ._shared import (
    TZ_LOCAL,
    _camera_name,
    _get_camera_list,
    _resolve_camera,
)

logger = logging.getLogger("dashboard.ai")

# Mirror the recorder's own RECORDING_DIR default so the lookup path stays
# in sync with where ffmpeg actually wrote segments. Previously this was
# hardcoded to `/data/recordings`, which worked in default deployments by
# coincidence (same as docker-compose's bind mount target). Anyone setting
# `RECORDING_DIR` to a non-default path — NAS mount, external archive disk —
# would have this tool silently return "no recordings" while the recorder
# wrote happily to the actual path.
RECORDING_DIR = os.environ.get("RECORDING_DIR", "/data/recordings")


SCHEMA = {'type': 'function', 'function': {'name': 'find_dvr_segment', 'description': "Find the DVR (.ts) recording segment that covers a given camera + date + time, and return a deep-link URL so the user can open it in the DVR tab. Use this when the user asks to see/review past footage (e.g. 'show me yesterday's busiest hour', 'I want to see the clip from 1pm'). DOES NOT extract or send video — returns a clickable URL to the existing DVR tab. Recommended workflow: (1) call query_event_patterns to find the busy hour, (2) call this with that hour as `time`, (3) format the response's deep_link as a markdown link for the user to click.", 'parameters': {'type': 'object', 'properties': {'camera': {'type': 'string', 'description': "Camera id (e.g. 'cam1'). Omit for primary camera. Must be a SINGLE camera, not 'all'."}, 'date': {'type': 'string', 'description': "Date — 'today', 'yesterday', or YYYY-MM-DD. Default 'today'."}, 'time': {'type': 'string', 'description': "Hour or time to find (e.g. '13:00', '1:00 PM', '17'). Omit to list all segments for that day."}}, 'required': []}}}


def _tool_find_dvr_segment(args: dict) -> str:
    """Find the DVR (.ts) segment that covers a given camera + date + time.
    Returns the segment metadata and a deep-link URL that opens the DVR tab
    on ai.html pre-loaded to that segment.

    This tool does NOT extract or send video bytes — that's what the DVR tab
    is for. The AI's job is to tell the user *which* segment to watch and
    hand them a clickable link.
    """
    import re
    from pathlib import Path
    args = args or {}
    camera_arg = args.get('camera', '')
    date_str = (args.get('date') or '').strip() or 'today'
    time_str = (args.get('time') or '').strip()
    cam_ids = _resolve_camera(camera_arg)
    if not cam_ids:
        return json.dumps({'error': f"Unknown camera '{camera_arg}'. Use a registered camera id.", 'available': [c['id'] for c in _get_camera_list()]})
    cam = cam_ids[0]
    now = datetime.now(TZ_LOCAL)
    if date_str == 'today':
        target_date = now.date()
    elif date_str == 'yesterday':
        target_date = (now - timedelta(days=1)).date()
    else:
        try:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            return json.dumps({'error': f"Invalid date '{date_str}'. Use 'today', 'yesterday', or YYYY-MM-DD."})
    target_minutes = None
    if time_str:
        t = time_str.strip().upper().replace(' ', '')
        m = re.match('^(\\d{1,2})(?::(\\d{2}))?(AM|PM)?$', t)
        if not m:
            return json.dumps({'error': f"Invalid time '{time_str}'. Use HH:MM (24h) or H:MMam/pm."})
        hh = int(m.group(1))
        mm = int(m.group(2) or 0)
        ampm = m.group(3)
        if ampm == 'AM' and hh == 12:
            hh = 0
        elif ampm == 'PM' and hh < 12:
            hh += 12
        if not (0 <= hh < 24 and 0 <= mm < 60):
            return json.dumps({'error': f'Time out of range: {time_str}'})
        target_minutes = hh * 60 + mm
    day_dir = Path(RECORDING_DIR) / cam / str(target_date)
    if not day_dir.is_dir():
        return json.dumps({'error': f'No recordings found for {cam} on {target_date}', 'hint': 'Check /api/recordings/dates?camera=<id> for available dates.'})
    seg_re = re.compile('^(\\d{2})-(\\d{2})\\.ts$')
    segments = []
    for f in sorted(day_dir.iterdir()):
        m = seg_re.match(f.name)
        if not m or not f.is_file():
            continue
        h, mn = (int(m.group(1)), int(m.group(2)))
        ampm = "PM" if h >= 12 else "AM"
        segments.append({"filename": f.name, "start_minutes": h * 60 + mn, "start_label": f"{h % 12 or 12}:{mn:02d} {ampm}", "size_mb": round(f.stat().st_size / (1024 * 1024), 1)})
    if not segments:
        return json.dumps({'error': f'No .ts segments in {day_dir}'})
    deep_link_base = f'/ai.html?tab=recordings&camera={cam}&date={target_date}'
    if target_minutes is None:
        return json.dumps({"camera": cam, "date": str(target_date), "segments_available": len(segments), "segments": [{"filename": s["filename"], "starts_at": s["start_label"], "size_mb": s["size_mb"], "deep_link": f"{deep_link_base}&segment={s['filename']}"} for s in segments], "note": "Pass `time` (e.g. '13:00') to pick a single best-match segment."})
    best = None
    for s in segments:
        if s['start_minutes'] <= target_minutes:
            if best is None or s['start_minutes'] > best['start_minutes']:
                best = s
    if best is None:
        best = segments[0]
        note = f"Requested time {time_str} is before the first recording of the day ({best['start_label']}). Returning the earliest segment."
    else:
        note = f"Segment starts at {best['start_label']} and runs ~1 hour. Click the deep_link to open it in the DVR tab."
    return json.dumps({"camera": cam, "camera_name": _camera_name(cam), "date": str(target_date), "requested_time": time_str, "segment": best["filename"], "segment_starts_at": best["start_label"], "size_mb": best["size_mb"], "deep_link": f"{deep_link_base}&segment={best['filename']}", "note": note})
