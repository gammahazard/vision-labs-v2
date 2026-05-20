"""
routes/ai_tools/send_telegram.py — implementation + schema for the `send_telegram` tool.

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
import collections
import threading
import time


SCHEMA = {'type': 'function', 'function': {'name': 'send_telegram', 'description': 'Send a message to the user via Telegram right now. Can include a live camera snapshot or a 5-second video clip. When the user asks for media from a specific camera, pass `camera`; otherwise defaults to primary.', 'parameters': {'type': 'object', 'properties': {'message': {'type': 'string', 'description': 'The message text to send'}, 'include_snapshot': {'type': 'boolean', 'description': 'If true, attach the latest live camera frame to the message.'}, 'include_clip': {'type': 'boolean', 'description': 'If true, capture and attach a 5-second video clip from the camera.'}, 'camera': {'type': 'string', 'description': "Camera id (e.g. 'cam1') for the snapshot/clip. Default = primary. Ignored for text-only messages."}}, 'required': ['message']}}}


async def _tool_send_telegram(args: dict) -> str:
    """Send a Telegram message, optionally with a live snapshot or video clip."""
    from routes.notifications import send_text, send_photo, send_video, is_configured, get_latest_frame, build_clip
    message = args.get('message', '')
    if not message:
        return json.dumps({'error': 'No message provided'})
    if len(message) > 4000:
        return json.dumps({'error': 'Message too long (max 4000 chars)'})
    if not is_configured():
        return json.dumps({'error': 'Telegram not configured'})
    allowed, wait = _send_telegram_rate_check()
    if not allowed:
        return json.dumps({'error': f'Telegram rate limit hit ({_SEND_TG_MAX_PER_WINDOW} sends per {int(_SEND_TG_WINDOW_SEC)}s). Retry in {wait:.0f}s.'})
    cam_ids = _resolve_camera(args.get('camera', ''))
    source_camera = cam_ids[0] if cam_ids else ctx.CAMERA_ID
    source_camera_name = _camera_name(source_camera)
    try:
        include_clip = args.get('include_clip', False)
        include_snapshot = args.get('include_snapshot', False)
        if include_clip:
            clip = build_clip(duration=5.0, fps=10, camera_id=source_camera)
            if clip:
                msg_id = await send_video(clip, f'🎬 {message}')
                return json.dumps({'status': 'sent_with_clip', 'message': message, 'message_id': msg_id, 'source_camera_id': source_camera, 'source_camera_name': source_camera_name})
            else:
                await send_text(f'{message}\n\n(Video clip unavailable — camera may be offline)')
                return json.dumps({'status': 'sent_text_only', 'message': message, 'source_camera_id': source_camera, 'note': 'Clip capture failed'})
        if include_snapshot:
            frame = get_latest_frame(camera_id=source_camera)
            if frame:
                msg_id = await send_photo(frame, message)
                return json.dumps({'status': 'sent_with_snapshot', 'message': message, 'message_id': msg_id, 'source_camera_id': source_camera, 'source_camera_name': source_camera_name})
            else:
                await send_text(f'{message}\n\n(Snapshot unavailable — camera may be offline)')
                return json.dumps({'status': 'sent_text_only', 'message': message, 'source_camera_id': source_camera, 'note': 'No frame available'})
        await send_text(message)
        return json.dumps({'status': 'sent', 'message': message})
    except Exception as e:
        return json.dumps({'error': str(e)})
