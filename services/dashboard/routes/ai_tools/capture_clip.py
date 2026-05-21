"""
routes/ai_tools/capture_clip.py — implementation + schema for the `capture_clip` tool.

Extracted from the legacy monolithic ai_tools.py (Phase J modularization).
The function and schema live together so adding/changing a tool is a single-
file change. ``__init__.py`` aggregates SCHEMA from every tool module into the
``TOOLS`` list that the chat endpoint passes to Ollama.
"""

import json
import logging
import os
from datetime import datetime

import routes as ctx
import routes.ai_state as ai_state

from ._shared import (
    TZ_LOCAL,
    _camera_key,
    _camera_name,
    _get_camera_list,
    _resolve_camera,
)

logger = logging.getLogger("dashboard.ai")


SCHEMA = {'type': 'function', 'function': {'name': 'capture_clip', 'description': "Record a 5-second LIVE video clip from a camera and show it in the chat. Use for 'show me what's happening right now'. For OLDER footage from past events (DVR), use find_dvr_segment instead — it returns a link to the right recording. Pass `camera` to pick one (default = primary).", 'parameters': {'type': 'object', 'properties': {'camera': {'type': 'string', 'description': "Camera id to record from (e.g. 'cam1', 'cam2'). Omit for the primary camera."}}, 'required': []}}}


def _tool_capture_clip(args: dict=None) -> str:
    """Capture 5-second MP4 clip from a live camera. Multi-camera aware."""
    from routes.notifications import build_clip
    from contracts.streams import STATE_KEY as _STATE_TMPL
    import uuid as _uuid
    args = args or {}
    camera_arg = args.get('camera', '')
    cam_ids = _resolve_camera(camera_arg)
    if not cam_ids:
        return json.dumps({'error': f"Unknown camera '{camera_arg}'. Use 'all' or a registered camera id.", 'available': [c['id'] for c in _get_camera_list()]})
    target_cam = cam_ids[0]
    try:
        mp4_bytes = build_clip(duration=5.0, fps=10, camera_id=target_cam)
        if not mp4_bytes:
            return json.dumps({'error': f"Clip capture failed on camera '{target_cam}' — may be offline or not enough frames"})
        clip_dir = os.path.join('/data/snapshots', 'clips')
        os.makedirs(clip_dir, exist_ok=True)
        filename = f"{datetime.now(TZ_LOCAL).strftime('%Y%m%d_%H%M%S')}_{target_cam}_{_uuid.uuid4().hex[:6]}.mp4"
        filepath = os.path.join(clip_dir, filename)
        raw_path = filepath + '.raw.mp4'
        with open(raw_path, 'wb') as f:
            f.write(mp4_bytes)
        import subprocess
        try:
            subprocess.run(['ffmpeg', '-y', '-i', raw_path, '-c:v', 'libx264', '-preset', 'ultrafast', '-movflags', '+faststart', '-an', filepath], capture_output=True, timeout=15)
            os.unlink(raw_path)
        except Exception:
            os.rename(raw_path, filepath)
        ai_state.stash_clip(filename)
        context = {'camera': target_cam, 'camera_name': _camera_name(target_cam)}
        try:
            state_key = _camera_key(_STATE_TMPL, target_cam)
            state = ctx.r.hgetall(state_key)
            if state:
                context['people_in_frame'] = int(state.get('num_people', 0))
                persons_raw = state.get('people', '[]')
                try:
                    persons = json.loads(persons_raw)
                    if persons:
                        context['persons'] = persons
                except (json.JSONDecodeError, TypeError):
                    pass
        except Exception:
            pass
        now = datetime.now(TZ_LOCAL)
        context['timestamp'] = now.strftime('%I:%M %p, %B %d %Y')
        context['duration_seconds'] = 5
        context['size_kb'] = round(len(mp4_bytes) / 1024, 1)
        return json.dumps({'clip_captured': True, 'context': context, 'instruction': 'A 5-second video clip has been recorded and will be shown to the user automatically. Describe what you know from the scene context. Do NOT try to embed the video data.'})
    except Exception as e:
        return json.dumps({'error': str(e)})
