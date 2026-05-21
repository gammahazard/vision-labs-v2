"""
routes/ai_tools/query_activity_heatmap.py — implementation + schema for the `query_activity_heatmap` tool.

Extracted from the legacy monolithic ai_tools.py (Phase J modularization).
The function and schema live together so adding/changing a tool is a single-
file change. ``__init__.py`` aggregates SCHEMA from every tool module into the
``TOOLS`` list that the chat endpoint passes to Ollama.
"""

import json
import logging
from datetime import datetime, timedelta

import routes as ctx

from ._shared import (
    TZ_LOCAL,
    _camera_key,
    _get_camera_list,
    _resolve_camera,
)

logger = logging.getLogger("dashboard.ai")


SCHEMA = {'type': 'function', 'function': {'name': 'query_activity_heatmap', 'description': "Get a day-of-week × hour-of-day activity heatmap. Shows which days and hours are busiest, weekend vs weekday comparison, and peak activity windows. **Defaults to camera='all' if not specified.**", 'parameters': {'type': 'object', 'properties': {'days_back': {'type': 'integer', 'description': 'How many days of history to analyze (default 14, max 30)'}, 'camera': {'type': 'string', 'description': "Camera id to analyze (e.g. 'cam1', 'cam2'), or 'all' for every camera. Default = primary camera."}}, 'required': []}}}


def _tool_query_activity_heatmap(args: dict) -> str:
    """Day-of-week × hour-of-day activity heatmap. Multi-camera aware (aggregates)."""
    from collections import defaultdict
    from contracts.streams import EVENT_STREAM as _EVT_TMPL
    days_back = min(int(args.get('days_back', 14)), 30)
    camera_arg = args.get('camera', 'all')
    cam_ids = _resolve_camera(camera_arg)
    if not cam_ids:
        return json.dumps({'error': f"Unknown camera '{camera_arg}'. Use 'all' or a registered camera id.", 'available': [c['id'] for c in _get_camera_list()]})
    now = datetime.now(TZ_LOCAL)
    start_date = now - timedelta(days=days_back)
    start_ms = int(start_date.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)
    try:
        events_raw = []
        for cid in cam_ids:
            evt_key = _camera_key(_EVT_TMPL, cid)
            events_raw.extend(ctx.r.xrange(evt_key, min=f'{start_ms}-0', max=f'{end_ms}-0'))
        DAY_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        heatmap = {d: defaultdict(int) for d in DAY_NAMES}
        hourly_total = defaultdict(int)
        daily_total = defaultdict(int)
        weekday_count = 0
        weekend_count = 0
        for msg_id, data in events_raw:
            ts = data.get('timestamp') or data.get('first_seen', '')
            try:
                if '.' in str(ts):
                    dt = datetime.fromtimestamp(float(ts), tz=TZ_LOCAL)
                else:
                    dt = datetime.fromisoformat(str(ts))
                day_name = DAY_NAMES[dt.weekday()]
                hour = dt.hour
                heatmap[day_name][hour] += 1
                hourly_total[hour] += 1
                daily_total[day_name] += 1
                if dt.weekday() < 5:
                    weekday_count += 1
                else:
                    weekend_count += 1
            except (ValueError, TypeError, OSError):
                continue
        peak_hour = max(hourly_total.items(), key=lambda x: x[1]) if hourly_total else (0, 0)
        peak_day = max(daily_total.items(), key=lambda x: x[1]) if daily_total else ('none', 0)
        grid = {}
        for day in DAY_NAMES:
            grid[day] = {f'{h:02d}:00': heatmap[day].get(h, 0) for h in range(24)}
        num_weekdays = max(min(days_back, 30) * 5 // 7, 1)
        num_weekends = max(min(days_back, 30) * 2 // 7, 1)
        return json.dumps({'cameras_queried': cam_ids, 'days_analyzed': days_back, 'total_events': len(events_raw), 'peak_hour': f'{peak_hour[0]:02d}:00 ({peak_hour[1]} events)', 'busiest_day': f'{peak_day[0]} ({peak_day[1]} events)', 'weekday_total': weekday_count, 'weekend_total': weekend_count, 'weekday_avg_per_day': round(weekday_count / num_weekdays, 1), 'weekend_avg_per_day': round(weekend_count / num_weekends, 1), 'heatmap': grid})
    except Exception as e:
        return json.dumps({'error': str(e)})
