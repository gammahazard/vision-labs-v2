"""
routes/ai_tools/query_event_patterns.py — implementation + schema for the `query_event_patterns` tool.

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


SCHEMA = {'type': 'function', 'function': {'name': 'query_event_patterns', 'description': "Analyze event patterns and trends. **Defaults to camera='all', category='all'.** Use 'category' to filter — when user says 'only people / no vehicles / just faces', pass category='people' (person_appeared, person_left, person_identified) NOT category='all'. Hourly analysis returns: busiest_hour, top_hours (top 5 with full breakdown), active_window (first→last non-zero hour), hourly_breakdown (all 24 hrs), by_type_per_hour, by_identity_per_hour, per_camera_hourly. Use 'date' arg to scope to ONE day (today/yesterday/YYYY-MM-DD); use 'days_back' for rolling window. The 'scope' field in the response echoes the active filters back to you.", 'parameters': {'type': 'object', 'properties': {'analysis_type': {'type': 'string', 'enum': ['hourly', 'daily', 'type_breakdown'], 'description': "Type of analysis: 'hourly' (by hour of day), 'daily' (by day), 'type_breakdown' (events by type)"}, 'date': {'type': 'string', 'description': "Optional: scope to ONE day — 'today', 'yesterday', or YYYY-MM-DD. Overrides days_back if set."}, 'days_back': {'type': 'integer', 'description': "How many days of history to analyze when 'date' is not set (default 7, max 30)"}, 'camera': {'type': 'string', 'description': "Camera id (e.g. 'cam1') or 'all' for every camera. Default = 'all'."}, 'category': {'type': 'string', 'enum': ['people', 'vehicles', 'faces', 'actions', 'security', 'all'], 'description': "Filter events by category. 'people' = person events only (appearances/identifications/departures, NO faces or vehicles). 'vehicles' = vehicle events only. 'faces' = face enrollments + reconciliations + identifications. Default = 'all'."}}, 'required': ['analysis_type']}}}


def _tool_query_event_patterns(args: dict) -> str:
    """Analyze event patterns for trends. Multi-camera aware (aggregates across requested cameras)."""
    from collections import defaultdict
    from contracts.streams import EVENT_STREAM as _EVT_TMPL
    analysis_type = args.get('analysis_type', 'hourly')
    days_back = min(int(args.get('days_back', 7)), 30)
    camera_arg = args.get('camera', 'all')
    date_str = args.get('date', '').strip()
    category = (args.get('category') or 'all').strip().lower()
    if category not in EVENT_CATEGORIES:
        return json.dumps({'error': f"Unknown category '{category}'. Valid: {list(EVENT_CATEGORIES.keys())}"})
    cam_ids = _resolve_camera(camera_arg)
    if not cam_ids:
        return json.dumps({'error': f"Unknown camera '{camera_arg}'. Use 'all' or a registered camera id.", 'available': [c['id'] for c in _get_camera_list()]})
    now = datetime.now(TZ_LOCAL)
    if date_str:
        if date_str == 'today':
            target_date = now.date()
        elif date_str == 'yesterday':
            target_date = (now - timedelta(days=1)).date()
        else:
            try:
                target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            except ValueError:
                return json.dumps({'error': f"Invalid date '{date_str}'. Use YYYY-MM-DD, 'today', or 'yesterday'."})
        day_start = datetime.combine(target_date, datetime.min.time(), tzinfo=TZ_LOCAL)
        day_end = datetime.combine(target_date, datetime.max.time(), tzinfo=TZ_LOCAL)
        start_ms = int(day_start.timestamp() * 1000)
        end_ms = int(day_end.timestamp() * 1000)
        scope_label = f'date={target_date}'
    else:
        start_date = now - timedelta(days=days_back)
        start_ms = int(start_date.timestamp() * 1000)
        end_ms = int(now.timestamp() * 1000)
        scope_label = f'last {days_back} days'
    if category != 'all':
        scope_label += f' · category={category} (event_types={list(EVENT_CATEGORIES[category])})'
    try:
        events_raw = []
        events_per_cam: dict[str, list] = {}
        for cid in cam_ids:
            evt_key = _camera_key(_EVT_TMPL, cid)
            cam_evts_all = ctx.r.xrange(evt_key, min=f'{start_ms}-0', max=f'{end_ms}-0')
            if category != 'all':
                cam_evts = [(mid, data) for mid, data in cam_evts_all if _category_matches(data.get('event_type', ''), category)]
            else:
                cam_evts = cam_evts_all
            events_per_cam[cid] = cam_evts
            events_raw.extend(cam_evts)
        if analysis_type == 'hourly':
            hourly = defaultdict(int)
            hourly_by_type = defaultdict(lambda: defaultdict(int))
            hourly_by_identity = defaultdict(lambda: defaultdict(int))
            hourly_per_cam = {cid: defaultdict(int) for cid in cam_ids}

            def _parse_hour(ts_raw):
                ts_str = str(ts_raw)
                try:
                    if '.' in ts_str:
                        return datetime.fromtimestamp(float(ts_str), tz=TZ_LOCAL).hour
                    return datetime.fromisoformat(ts_str).hour
                except (ValueError, TypeError, OSError):
                    return None
            for cid, cam_evts in events_per_cam.items():
                for msg_id, data in cam_evts:
                    hour = _parse_hour(data.get('timestamp') or data.get('first_seen', ''))
                    if hour is None:
                        continue
                    hourly[hour] += 1
                    hourly_per_cam[cid][hour] += 1
                    etype = data.get('event_type', 'unknown')
                    hourly_by_type[hour][etype] += 1
                    if etype == 'person_identified':
                        name = data.get('identity_name') or '<unknown>'
                        hourly_by_identity[hour][name] += 1
            hourly_breakdown = {f'{h:02d}:00': hourly.get(h, 0) for h in range(24)}
            by_type_per_hour = {f'{h:02d}:00': dict(hourly_by_type.get(h, {})) for h in range(24)}
            by_identity_per_hour = {f'{h:02d}:00': dict(hourly_by_identity.get(h, {})) for h in range(24)}
            per_camera_hourly = {cid: {f'{h:02d}:00': hourly_per_cam[cid].get(h, 0) for h in range(24)} for cid in cam_ids}
            ranked = sorted(hourly.items(), key=lambda x: x[1], reverse=True)
            top_hours = [{'hour': f'{h:02d}:00', 'count': cnt, 'by_type': dict(hourly_by_type.get(h, {})), 'by_identity': dict(hourly_by_identity.get(h, {})), 'per_camera': {cid: hourly_per_cam[cid].get(h, 0) for cid in cam_ids}} for h, cnt in ranked[:5] if cnt > 0]
            active_hours = sorted([h for h, c in hourly.items() if c > 0])
            if active_hours:
                active_window = f'{active_hours[0]:02d}:00–{active_hours[-1]:02d}:00'
                quiet_hours = [f'{h:02d}:00' for h in range(24) if hourly.get(h, 0) == 0]
            else:
                active_window = 'no activity'
                quiet_hours = [f'{h:02d}:00' for h in range(24)]
            busiest = ranked[0] if ranked and ranked[0][1] > 0 else (0, 0)
            # Detection count per hour (one entry per session start, not per
            # event). For people: person_appeared. For vehicles: vehicle_detected.
            # Surfacing this so the LLM can answer "how many were detected at
            # busiest hour" without double-counting the matching _left events.
            detections_per_hour = {
                f'{h:02d}:00': hourly_by_type.get(h, {}).get('person_appeared', 0)
                               + hourly_by_type.get(h, {}).get('vehicle_detected', 0)
                for h in range(24)
            }
            busiest_hour_detections = detections_per_hour.get(f'{busiest[0]:02d}:00', 0) if busiest[1] > 0 else 0
            return json.dumps({
                'analysis': 'hourly',
                'scope': scope_label,
                'cameras_queried': cam_ids,
                'days_analyzed': days_back if not date_str else 1,
                'total_events': len(events_raw),
                'busiest_hour': f'{busiest[0]:02d}:00 ({busiest[1]} events)',
                'busiest_hour_detections': busiest_hour_detections,
                'busiest_hour_detection_note': "busiest_hour_detections counts person_appeared + vehicle_detected only (one entry per session start). Use this for 'how many detections in the busiest hour'.",
                'top_hours': top_hours,
                'active_window': active_window,
                'quiet_hours_count': len(quiet_hours),
                'hourly_breakdown': hourly_breakdown,
                'detections_per_hour': detections_per_hour,
                'by_type_per_hour': by_type_per_hour,
                'by_identity_per_hour': by_identity_per_hour,
                'per_camera_hourly': per_camera_hourly,
            })
        elif analysis_type == 'daily':
            daily = defaultdict(int)
            daily_by_type = defaultdict(lambda: defaultdict(int))
            for msg_id, data in events_raw:
                ts = data.get('timestamp') or data.get('first_seen', '')
                try:
                    if '.' in str(ts):
                        dt = datetime.fromtimestamp(float(ts), tz=TZ_LOCAL)
                    else:
                        dt = datetime.fromisoformat(str(ts))
                    day_key = dt.strftime('%Y-%m-%d')
                    daily[day_key] += 1
                    daily_by_type[day_key][data.get('event_type', 'unknown')] += 1
                except (ValueError, TypeError, OSError):
                    continue
            avg = sum(daily.values()) / max(len(daily), 1)
            return json.dumps({'analysis': 'daily', 'cameras_queried': cam_ids, 'days_analyzed': days_back, 'total_events': len(events_raw), 'daily_breakdown': dict(sorted(daily.items())), 'by_type_per_day': {d: dict(daily_by_type[d]) for d in sorted(daily_by_type.keys())}, 'daily_average': round(avg, 1), 'busiest_day': max(daily.items(), key=lambda x: x[1])[0] if daily else 'none'})
        elif analysis_type == 'type_breakdown':
            types = {t: 0 for t in KNOWN_EVENT_TYPES}
            for msg_id, data in events_raw:
                evt_type = data.get('event_type', 'unknown')
                types[evt_type] = types.get(evt_type, 0) + 1
            return json.dumps({'analysis': 'type_breakdown', 'cameras_queried': cam_ids, 'days_analyzed': days_back, 'total_events': len(events_raw), 'by_type': dict(sorted(types.items(), key=lambda x: x[1], reverse=True))})
        else:
            return json.dumps({'error': f'Unknown analysis type: {analysis_type}'})
    except Exception as e:
        return json.dumps({'error': str(e)})
