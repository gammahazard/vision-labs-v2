"""
routes/ai_tools/query_events.py — implementation + schema for the `query_events` tool.

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


SCHEMA = {'type': 'function', 'function': {'name': 'query_events', 'description': "Search the most recent security events (newest first, max 50). **Defaults to camera='all' if not specified.** Returns events plus by_type and by_identity aggregations so you don't have to count manually. NOTE: only shows the latest events — use query_events_by_date for a full day's totals.", 'parameters': {'type': 'object', 'properties': {'count': {'type': 'integer', 'description': 'Number of recent events to return (max 50)'}, 'event_type': {'type': 'string', 'description': f'Filter by event type. Known types: {KNOWN_EVENT_TYPES_DOC}. Leave empty for all.'}, 'camera': {'type': 'string', 'description': "Camera id to query (e.g. 'cam1', 'cam2'), or 'all' for every camera. Default = primary camera."}}, 'required': []}}}


def _tool_query_events(args: dict) -> str:
    """Query recent events from Redis. Defaults to ALL cameras when no camera arg
    is passed (analytical tool — cross-camera is the useful answer)."""
    from contracts.streams import EVENT_STREAM as _EVT_TMPL
    count = min(int(args.get('count', 20)), 50)
    event_type = args.get('event_type', '')
    camera_arg = args.get('camera', 'all')
    cam_ids = _resolve_camera(camera_arg)
    if not cam_ids:
        return json.dumps({'error': f"Unknown camera id: '{camera_arg}'", 'available': [c['id'] for c in _get_camera_list()]})
    try:
        all_events = []
        for cid in cam_ids:
            stream = _camera_key(_EVT_TMPL, cid)
            events_raw = ctx.r.xrevrange(stream, count=count)
            for msg_id, data in events_raw:
                evt = {k: v for k, v in data.items()}
                evt['event_id'] = msg_id
                evt['camera_id'] = cid
                evt['camera_name'] = _camera_name(cid)
                if event_type and evt.get('event_type') != event_type:
                    continue
                all_events.append(evt)
        all_events.sort(key=lambda e: e.get('event_id', ''), reverse=True)
        capped = all_events[:count]
        by_type = {}
        by_identity = {}
        for evt in capped:
            t = evt.get('event_type', 'unknown')
            by_type[t] = by_type.get(t, 0) + 1
            if t == 'person_identified':
                name = evt.get('identity_name') or '<unknown>'
                by_identity[name] = by_identity.get(name, 0) + 1
        return json.dumps({'events': capped, 'showing_count': len(capped), 'limit_requested': count, 'by_type': by_type, 'by_identity': by_identity, 'unique_people_identified': len(by_identity), 'cameras_queried': cam_ids, 'note': "This shows only the most recent events (max 50). Use query_events_by_date for a full day's totals."})
    except Exception as e:
        return json.dumps({'error': str(e)})
