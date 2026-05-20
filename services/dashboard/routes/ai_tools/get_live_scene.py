"""
routes/ai_tools/get_live_scene.py — implementation + schema for the `get_live_scene` tool.

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


SCHEMA = {'type': 'function', 'function': {'name': 'get_live_scene', 'description': "Get what's happening on camera RIGHT NOW — who is in frame, their actions, how long they've been there.", 'parameters': {'type': 'object', 'properties': {}, 'required': []}}}


def _tool_get_live_scene() -> str:
    """Aggregate the current scene from every registered camera.

    Always multi-camera-aware — no `camera` arg. The LLM gets a per-camera
    breakdown ('Front: 2 people · Basement: 0') so it can pick which one to
    talk about without needing to call this tool multiple times.
    """
    from contracts.streams import STATE_KEY as _STATE_TMPL, IDENTITY_KEY as _IDKEY_TMPL
    cam_ids = _resolve_camera('all')
    if not cam_ids:
        cam_ids = [ctx.CAMERA_ID]
    cameras_data = []
    total_people = 0
    for cid in cam_ids:
        state_key = _camera_key(_STATE_TMPL, cid)
        id_key = _camera_key(_IDKEY_TMPL, cid)
        cam_block = {'id': cid, 'name': _camera_name(cid)}
        try:
            state = ctx.r.hgetall(state_key)
            if state:
                n = int(state.get('num_people', '0'))
                cam_block['num_people'] = n
                total_people += n
                try:
                    persons = json.loads(state.get('people', '[]'))
                    if persons:
                        cam_block['persons'] = persons
                except (json.JSONDecodeError, TypeError):
                    pass
            else:
                cam_block['num_people'] = 0
            id_state = ctx.r.hgetall(id_key)
            if id_state and id_state.get('identities'):
                try:
                    cam_block['identities'] = json.loads(id_state['identities'])
                except (json.JSONDecodeError, TypeError):
                    pass
        except Exception as e:
            cam_block['error'] = str(e)
        cameras_data.append(cam_block)
    if not cameras_data:
        return json.dumps({'scene': 'No camera data available — registry empty or tracker not running.'})
    identified_set = set()
    for cb in cameras_data:
        for ident in cb.get('identities', []) or []:
            if isinstance(ident, dict):
                name = ident.get('name') or ident.get('identity_name')
                if name and name != 'unknown':
                    identified_set.add(name)
    identified_people = sorted(identified_set)
    primary_block = next((c for c in cameras_data if c['id'] == ctx.CAMERA_ID), cameras_data[0])
    out = {'cameras': cameras_data, 'total_people_across_cameras': total_people, 'identified_people_now': identified_people, 'identified_people_count': len(identified_people), 'num_people': primary_block.get('num_people', 0)}
    if 'persons' in primary_block:
        out['persons'] = primary_block['persons']
    return json.dumps(out)
