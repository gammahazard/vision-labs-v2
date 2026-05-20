"""
routes/ai_tools/capture_snapshot.py — implementation + schema for the `capture_snapshot` tool.

Extracted from the legacy monolithic ai_tools.py (Phase J modularization).
The function and schema live together so adding/changing a tool is a single-
file change. ``__init__.py`` aggregates SCHEMA from every tool module into the
``TOOLS`` list that the chat endpoint passes to Ollama.
"""

import json
import logging
import os
from datetime import datetime, timedelta

import routes as ctx
import routes.ai_state as ai_state

from ._shared import (
    KNOWN_EVENT_TYPES,
    KNOWN_EVENT_TYPES_DOC,
    EVENT_CATEGORIES,
    TZ_LOCAL,
    _category_matches,
    _camera_key,
    _camera_name,
    _get_camera_list,
    _redact_sensitive,
    _resolve_camera,
)

logger = logging.getLogger("dashboard.ai")


SCHEMA = {'type': 'function', 'function': {'name': 'capture_snapshot', 'description': "Capture the current camera frame, show it in the chat, AND run the MiniCPM-V vision model on it to get a real visual description. Use this for ANY 'what do you see / what's happening / who is there / what are they doing' question — it both displays the image and tells you what's actually visible. Returns vision_analysis (visual description), context (weather + tracker state), source_camera. Pass `camera` to pick one (default = primary). Pass describe=false to skip vision (~3s faster) only if you just need the picture without a description.", 'parameters': {'type': 'object', 'properties': {'camera': {'type': 'string', 'description': "Camera id to capture (e.g. 'cam1', 'cam2'). Omit for the primary camera."}, 'describe': {'type': 'boolean', 'description': 'If true (default), also run MiniCPM-V on the frame and return its description in vision_analysis. Set false to skip for ~3s faster response when you only need the image.'}}, 'required': []}}}


async def _tool_capture_snapshot(args: dict=None) -> str:
    """Capture camera frame WITH automatic visual analysis from MiniCPM-V.

    Returns: snapshot shown to user, tracker state context, AND a free-text
    visual description from the vision model so the chat LLM doesn't have to
    describe blindly. Vision pass runs by default — pass describe=false only
    if you want a raw snapshot without the ~3s vision inference.
    """
    args = args or {}
    import base64
    import httpx
    from routes.notifications import get_latest_frame, describe_scene
    cam_ids = _resolve_camera(args.get('camera', ''))
    if not cam_ids:
        return json.dumps({'error': f"Unknown camera id: '{args.get('camera')}'", 'available': [c['id'] for c in _get_camera_list()]})
    describe = args.get('describe', True)
    if isinstance(describe, str):
        describe = describe.lower() not in ('false', '0', 'no')
    snap_camera = cam_ids[0] if len(cam_ids) == 1 else ctx.CAMERA_ID
    try:
        frame = get_latest_frame(camera_id=snap_camera)
        if not frame:
            return json.dumps({'error': f"No frame available for camera '{snap_camera}' — may be offline"})
        b64 = base64.b64encode(frame).decode('utf-8')
        ai_state.stash_snapshot(b64)
        context = {'source_camera_id': snap_camera, 'source_camera_name': _camera_name(snap_camera)}
        try:
            api_key = os.getenv('OPENWEATHER_API_KEY', '')
            lat = os.getenv('LOCATION_LAT', '')
            lon = os.getenv('LOCATION_LON', '')
            if api_key and lat and lon:
                async with httpx.AsyncClient() as client:
                    resp = await client.get('https://api.openweathermap.org/data/2.5/weather', params={'lat': lat, 'lon': lon, 'appid': api_key, 'units': 'metric'}, timeout=3)
                if resp.status_code == 200:
                    w = resp.json()
                    context['weather'] = {'temp_c': round(w['main']['temp']), 'feels_like_c': round(w['main']['feels_like']), 'description': w['weather'][0]['description'], 'humidity': w['main']['humidity'], 'wind_kmh': round(w['wind']['speed'] * 3.6)}
        except Exception:
            pass
        try:
            from contracts.streams import STATE_KEY as _STATE_TMPL
            state_key = _camera_key(_STATE_TMPL, snap_camera)
            state = ctx.r.hgetall(state_key)
            if state:
                context['scene'] = {'people_in_frame': int(state.get('num_people', 0))}
                persons_raw = state.get('people', '[]')
                try:
                    persons = json.loads(persons_raw)
                    if persons:
                        context['scene']['persons'] = persons
                except (json.JSONDecodeError, TypeError):
                    pass
        except Exception:
            pass
        now = datetime.now(TZ_LOCAL)
        context['timestamp'] = now.strftime('%I:%M %p, %B %d %Y')
        context['time_period'] = 'night' if now.hour < 6 or now.hour >= 21 else 'day' if 8 <= now.hour < 18 else 'twilight'
        visual_description = ''
        if describe:
            try:
                visual_description = await describe_scene(frame, prompt='Describe what you see in this security camera image. Mention people, vehicles, activity, lighting, and anything notable. Be concise — 2-3 sentences.', timeout=30.0) or ''
            except Exception as e:
                logger.warning(f'capture_snapshot vision pass failed: {e}')
        out = {'snapshot_captured': True, 'size_kb': round(len(frame) / 1024, 1), 'context': context, 'instruction': "A live camera snapshot has been captured and will be shown to the user automatically. The 'vision_analysis' field (if present) is a description from the MiniCPM-V vision model of what the camera actually sees — use it to answer the user's question instead of guessing from tracker state. Do NOT output base64 or reference the image data directly."}
        if visual_description:
            out['vision_analysis'] = visual_description
        elif describe:
            out['vision_analysis'] = '(vision model unavailable or timed out)'
        return json.dumps(out)
    except Exception as e:
        return json.dumps({'error': str(e)})
