"""
routes/ai_tools/get_system_status.py — implementation + schema for the `get_system_status` tool.

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


SCHEMA = {'type': 'function', 'function': {'name': 'get_system_status', 'description': 'Get current system status: stream sizes, config settings, notification preferences. Aggregates across all cameras by default; pass `camera` to scope to one.', 'parameters': {'type': 'object', 'properties': {'camera': {'type': 'string', 'description': "Camera id to scope status to (e.g. 'cam1', 'cam2'), or 'all'. Default = 'all' (aggregate across cameras)."}}, 'required': []}}}


def _tool_get_system_status(args: dict=None) -> str:
    """Get system status from Redis. Multi-camera aware.

    Default = aggregate across all enabled cameras (most useful for "how's
    the system?" type queries). Pass camera=<id> to scope to one.
    """
    from contracts.streams import EVENT_STREAM as _EVT_TMPL, CONFIG_KEY as _CFG_TMPL, STATE_KEY as _STATE_TMPL
    args = args or {}
    camera_arg = args.get('camera', '')
    if not camera_arg:
        camera_arg = 'all'
    cam_ids = _resolve_camera(camera_arg)
    if not cam_ids:
        return json.dumps({'error': f"Unknown camera '{camera_arg}'. Use 'all' or a registered camera id.", 'available': [c['id'] for c in _get_camera_list()]})
    try:
        per_camera = {}
        total_events = 0
        for cid in cam_ids:
            evt_key = _camera_key(_EVT_TMPL, cid)
            cfg_key = _camera_key(_CFG_TMPL, cid)
            state_key = _camera_key(_STATE_TMPL, cid)
            ev_len = ctx.r.xlen(evt_key)
            per_camera[cid] = {'name': _camera_name(cid), 'events_in_stream': ev_len, 'config': _redact_sensitive(ctx.r.hgetall(cfg_key)), 'state': ctx.r.hgetall(state_key)}
            total_events += ev_len
        if len(cam_ids) == 1:
            entry = per_camera[cam_ids[0]]
            return json.dumps({'camera': cam_ids[0], 'camera_name': entry['name'], 'events_in_stream': entry['events_in_stream'], 'config': entry['config'], 'state': entry['state']})
        return json.dumps({'cameras_queried': cam_ids, 'total_events_across_cameras': total_events, 'per_camera': per_camera})
    except Exception as e:
        return json.dumps({'error': str(e)})
