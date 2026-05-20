"""
routes/ai_tools/query_zones.py — implementation + schema for the `query_zones` tool.

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


SCHEMA = {'type': 'function', 'function': {'name': 'query_zones', 'description': "List security zones defined on a camera, including their names, alert levels, and point coordinates. Zones are per-camera. Pass `camera` to pick one, or 'all' to list across every camera grouped by camera (default = primary).", 'parameters': {'type': 'object', 'properties': {'camera': {'type': 'string', 'description': "Camera id whose zones to list (e.g. 'cam1', 'cam2'), or 'all' for every camera grouped. Default = primary camera."}}, 'required': []}}}


def _tool_query_zones(args: dict=None) -> str:
    """List security zones defined per camera. Multi-camera aware."""
    from contracts.streams import ZONE_KEY as _ZONE_TMPL
    args = args or {}
    camera_arg = args.get('camera', '')
    cam_ids = _resolve_camera(camera_arg)
    if not cam_ids:
        return json.dumps({'error': f"Unknown camera '{camera_arg}'. Use 'all' or a registered camera id.", 'available': [c['id'] for c in _get_camera_list()]})
    try:
        per_camera = {}
        total = 0
        for cid in cam_ids:
            zone_key = _camera_key(_ZONE_TMPL, cid)
            zone_data = ctx.r.hgetall(zone_key) or {}
            zones = []
            for zone_id, zone_json in zone_data.items():
                try:
                    zone = json.loads(zone_json)
                    zone['id'] = zone_id
                    zones.append(zone)
                except (json.JSONDecodeError, TypeError):
                    zones.append({'id': zone_id, 'raw': zone_json})
            per_camera[cid] = {'name': _camera_name(cid), 'zones': zones, 'count': len(zones)}
            total += len(zones)
        if len(cam_ids) == 1:
            cid = cam_ids[0]
            entry = per_camera[cid]
            return json.dumps({'camera': cid, 'camera_name': entry['name'], 'zones': entry['zones'], 'count': entry['count'], 'message': 'No zones defined yet.' if entry['count'] == 0 else None})
        return json.dumps({'cameras_queried': cam_ids, 'total_zones': total, 'per_camera': per_camera})
    except Exception as e:
        return json.dumps({'error': str(e)})
