"""
routes/ai_tools/query_notification_history.py — implementation + schema for the `query_notification_history` tool.

Extracted from the legacy monolithic ai_tools.py (Phase J modularization).
The function and schema live together so adding/changing a tool is a single-
file change. ``__init__.py`` aggregates SCHEMA from every tool module into the
``TOOLS`` list that the chat endpoint passes to Ollama.
"""

import json
import logging

import routes as ctx

from ._shared import (
    _camera_key,
    _camera_name,
    _get_camera_list,
    _resolve_camera,
)

logger = logging.getLogger("dashboard.ai")


SCHEMA = {'type': 'function', 'function': {'name': 'query_notification_history', 'description': "Get recent Telegram notifications that were sent by the system. Returns notifications plus by_type and by_identity aggregations. Pass `camera` to scope to one camera or 'all' (default = 'all').", 'parameters': {'type': 'object', 'properties': {'count': {'type': 'integer', 'description': 'Number of recent notifications to return (default 20, max 50)'}, 'camera': {'type': 'string', 'description': "Camera id (e.g. 'cam1') or 'all'. Default = 'all'."}}, 'required': []}}}


def _tool_query_notification_history(args: dict) -> str:
    """Get recent notification records (events that triggered Telegram alerts). Multi-camera aware."""
    from contracts.streams import EVENT_STREAM as _EVT_TMPL
    count = min(int(args.get('count', 20)), 50)
    camera_arg = args.get('camera', '')
    cam_ids = _resolve_camera(camera_arg)
    if not cam_ids:
        return json.dumps({'error': f"Unknown camera '{camera_arg}'. Use 'all' or a registered camera id.", 'available': [c['id'] for c in _get_camera_list()]})
    try:
        SWEEP_PER_CAM = max(count * 10, 200)
        all_alerts = []
        scanned_total = 0
        for cid in cam_ids:
            evt_key = _camera_key(_EVT_TMPL, cid)
            events_raw = ctx.r.xrevrange(evt_key, count=SWEEP_PER_CAM)
            scanned_total += len(events_raw)
            for msg_id, data in events_raw:
                if data.get('alert_triggered') in ('true', '1'):
                    all_alerts.append({'event_id': msg_id, 'camera_id': cid, 'camera_name': _camera_name(cid), 'type': data.get('event_type', 'unknown'), 'person_id': data.get('person_id', ''), 'identity': data.get('identity_name', ''), 'zone': data.get('zone', ''), 'timestamp': data.get('timestamp', ''), 'alert_level': data.get('alert_level', '')})
        all_alerts.sort(key=lambda a: a['event_id'], reverse=True)
        capped = all_alerts[:count]
        by_type = {}
        by_identity = {}
        for a in all_alerts:
            t = a['type']
            by_type[t] = by_type.get(t, 0) + 1
            if a['identity']:
                by_identity[a['identity']] = by_identity.get(a['identity'], 0) + 1
        return json.dumps({'notifications': capped, 'alerts_shown': len(capped), 'alerts_found_in_sweep': len(all_alerts), 'events_scanned_per_camera': SWEEP_PER_CAM, 'events_scanned_total': scanned_total, 'by_type': by_type, 'by_identity': by_identity, 'cameras_queried': cam_ids, 'truncated': len(all_alerts) > count, 'note': f'Showing newest {len(capped)} of {len(all_alerts)} alerts found in the most recent {SWEEP_PER_CAM} events per camera. For older alerts, the Redis stream may have trimmed them — use query_events_by_date for a specific date.'})
    except Exception as e:
        return json.dumps({'error': str(e)})
