"""
routes/ai_tools/browse_vehicles.py — implementation + schema for the `browse_vehicles` tool.

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
    TZ_LOCAL,
    _get_camera_list,
    _resolve_camera,
)

logger = logging.getLogger("dashboard.ai")


SCHEMA = {'type': 'function', 'function': {'name': 'browse_vehicles', 'description': 'Browse vehicle detection snapshots for a given day. Shows snapshot images inline in the chat. Use when the user asks to see vehicle photos or detections. Vehicle detection currently only runs on cameras with `detect_vehicles=true` in the registry.', 'parameters': {'type': 'object', 'properties': {'date': {'type': 'string', 'description': "Date in YYYY-MM-DD format, or 'today'/'yesterday'. Defaults to today."}, 'count': {'type': 'integer', 'description': 'Number of recent snapshots to show (default 5, max 10)'}, 'camera': {'type': 'string', 'description': "Camera id to scope to (e.g. 'cam1'), or 'all'. Default = primary camera. Only cameras with vehicle detection enabled will have snapshots."}}, 'required': []}}}


def _tool_browse_vehicles(args: dict) -> str:
    """List vehicle detection snapshots for a given day and stash images for display.

    Multi-camera note: vehicle snapshots are currently saved to a shared
    directory (`VEHICLE_SNAPSHOT_DIR/{date}/`), not per-camera. If a `camera`
    arg is passed, we validate it exists and that it has detect_vehicles=true
    in the registry. If detect_vehicles is false, return an empty result with
    an explanation rather than misleading snapshots from another camera.
    """
    import glob
    date_str = args.get('date', 'today')
    count_requested = min(int(args.get('count', 5)), 10)
    camera_arg = args.get('camera', '')
    cam_ids = _resolve_camera(camera_arg)
    if not cam_ids:
        return json.dumps({'error': f"Unknown camera '{camera_arg}'. Use 'all' or a registered camera id.", 'available': [c['id'] for c in _get_camera_list()]})
    cams_with_vehicles = []
    cams_without_vehicles = []
    for c in _get_camera_list():
        if c['id'] in cam_ids:
            if c.get('detect_vehicles', True):
                cams_with_vehicles.append(c['id'])
            else:
                cams_without_vehicles.append(c['id'])
    if camera_arg and camera_arg.lower() != 'all' and (not cams_with_vehicles):
        return json.dumps({'date': 'n/a', 'count': 0, 'snapshots': [], 'camera': camera_arg, 'message': f"Camera '{camera_arg}' does not have vehicle detection enabled (detect_vehicles=false). No vehicle snapshots will exist for it."})
    now = datetime.now(TZ_LOCAL)
    if date_str == 'today':
        target_date = now.strftime('%Y-%m-%d')
    elif date_str == 'yesterday':
        target_date = (now - timedelta(days=1)).strftime('%Y-%m-%d')
    else:
        target_date = date_str
    import re as _re
    if not _re.fullmatch('\\d{4}-\\d{2}-\\d{2}', target_date):
        return json.dumps({'error': f"Invalid date format '{target_date}'. Use YYYY-MM-DD or 'today'/'yesterday'."})
    snapshot_dir = ctx.VEHICLE_SNAPSHOT_DIR or '/data/snapshots/vehicles'
    candidate_dirs: list[tuple[str, str]] = []
    for cid in cams_with_vehicles:
        p = os.path.join(snapshot_dir, cid, target_date)
        if os.path.isdir(p):
            candidate_dirs.append((cid, p))
    if not camera_arg or camera_arg.lower() == 'all':
        legacy = os.path.join(snapshot_dir, target_date)
        if os.path.isdir(legacy):
            candidate_dirs.append(('', legacy))
    try:
        if not candidate_dirs:
            return json.dumps({'date': target_date, 'count': 0, 'snapshots': [], 'message': f'No vehicle snapshots for {target_date}'})
        snapshots = []
        for src_cam, day_dir in candidate_dirs:
            files = sorted(glob.glob(os.path.join(day_dir, '*.jpg')))
            for f in files:
                basename = os.path.basename(f)
                base_name = basename.rsplit('.', 1)[0]
                parts = base_name.split('_', 1)
                time_str = parts[0].replace('-', ':') if parts else ''
                vehicle_class = parts[1] if len(parts) > 1 else 'vehicle'
                cam_segment = src_cam if src_cam else '_legacy'
                snapshots.append({'filename': basename, 'time': time_str, 'vehicle_class': vehicle_class, 'camera': src_cam, 'size_kb': round(os.path.getsize(f) / 1024, 1), 'url': f'/api/browse/snapshot/{cam_segment}/{target_date}/{basename}'})
        snapshots.sort(key=lambda s: s.get('time', ''))
        display_snapshots = snapshots[-count_requested:]
        if display_snapshots:
            ai_state.stash_images([{"url": s["url"], "caption": f"{s['time']} — {s['vehicle_class']}"} for s in display_snapshots])
        layout_note = None
        if cams_without_vehicles:
            layout_note = f'Note: snapshots are saved to a shared directory, not per-camera. Cameras {cams_without_vehicles} have vehicle detection disabled and produce no snapshots. Cameras producing snapshots: {cams_with_vehicles}.'
        return json.dumps({'date': target_date, 'cameras_requested': cam_ids, 'cameras_with_vehicle_detection': cams_with_vehicles, 'count': len(snapshots), 'snapshots': snapshots[-20:], 'images_will_be_shown': len(display_snapshots), 'note': f'Showing last {min(20, len(snapshots))} of {len(snapshots)}' if len(snapshots) > 20 else None, 'layout_note': layout_note, 'instruction': 'The vehicle snapshot images will be displayed inline to the user automatically. Describe the snapshots using the metadata (timestamps, vehicle classes). Do NOT try to embed the images yourself.'})
    except Exception as e:
        return json.dumps({'error': str(e)})
