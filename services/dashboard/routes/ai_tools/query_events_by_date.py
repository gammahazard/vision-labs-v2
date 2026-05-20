"""
routes/ai_tools/query_events_by_date.py — implementation + schema for the `query_events_by_date` tool.

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


def _load_jsonl_journal(target_date) -> list:
    """Load every event written to /data/events/<YYYY-MM-DD>.jsonl.

    The event poller writes one JSON object per line to this file as the
    authoritative long-term record. The Redis events stream is capped at
    MAX_EVENT_STREAM_LEN (default 5000 per camera), so on a busy day the
    oldest events get trimmed from Redis — but they survive in JSONL.

    Returns a list of dicts shaped like the Redis stream entries (so the
    caller can merge them by event_id without special-casing). Silently
    returns [] if the file doesn't exist or any line fails to parse.
    """
    journal_path = f"/data/events/{target_date}.jsonl"
    if not os.path.isfile(journal_path):
        return []
    out = []
    try:
        with open(journal_path, "r") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (ValueError, json.JSONDecodeError):
                    continue  # skip corrupted lines (partial writes from crash)
                # Match the shape of (msg_id, data) used in the Redis branch.
                # JSONL uses "id" for stream id, "camera" for cam_id, and
                # has all other event fields at top level.
                evt = dict(entry)
                evt["event_id"] = evt.get("id") or evt.get("event_id") or ""
                evt["camera"] = evt.get("camera") or ""
                ts = evt.get("timestamp")
                if ts is not None:
                    evt["timestamp"] = str(ts)
                out.append(evt)
    except Exception:
        return []
    return out


SCHEMA = {'type': 'function', 'function': {'name': 'query_events_by_date', 'description': "Query events filtered by date. **Defaults to camera='all', category='all'.** Use 'category' to filter — when user says 'only people / no vehicles / just faces', pass category='people'. Use 'event_type' to filter to ONE specific event type. Returns: total_events, by_type, by_identity, unique_people_identified, latest_events, per_camera with each camera's own by_type and by_identity. Use total_events for 'how many detections', by_identity for 'who was seen', per_camera.<cam>.by_identity for 'which camera saw which person'. NEVER invent identity counts.", 'parameters': {'type': 'object', 'properties': {'date': {'type': 'string', 'description': "Date to query in YYYY-MM-DD format. Use 'today' or 'yesterday' as shortcuts."}, 'event_type': {'type': 'string', 'description': f"Optional: filter by ONE event type. Known types: {KNOWN_EVENT_TYPES_DOC}. Use 'category' instead if user said 'people' or 'vehicles' generally."}, 'category': {'type': 'string', 'enum': ['people', 'vehicles', 'faces', 'actions', 'security', 'all'], 'description': "Filter by category. 'people' = person events only. 'vehicles' = vehicle events only. 'faces' = face events + person_identified. Default 'all'."}, 'camera': {'type': 'string', 'description': "Camera id to query (e.g. 'cam1', 'cam2'), or 'all' for every camera. Default = primary camera."}}, 'required': ['date']}}}


def _tool_query_events_by_date(args: dict) -> str:
    """Query events filtered by date. Multi-camera aware — defaults to ALL cameras
    when no camera is specified (analytical tool; cross-camera is the useful answer).

    Merges the Redis events stream with the JSONL journal at
    /data/events/<date>.jsonl so requests for past dates still return data
    even if the Redis stream has trimmed those events (default cap 5000 per
    camera). Dedup is by event_id.
    """
    from contracts.streams import EVENT_STREAM as _EVT_TMPL
    date_str = args.get('date', 'today')
    event_type = args.get('event_type', '')
    camera_arg = args.get('camera', 'all')
    category = (args.get('category') or 'all').strip().lower()
    if category not in EVENT_CATEGORIES:
        return json.dumps({'error': f"Unknown category '{category}'. Valid: {list(EVENT_CATEGORIES.keys())}"})
    cam_ids = _resolve_camera(camera_arg)
    if not cam_ids:
        return json.dumps({'error': f"Unknown camera '{camera_arg}'. Use 'all' or a registered camera id.", 'available': [c['id'] for c in _get_camera_list()]})
    now = datetime.now(TZ_LOCAL)
    if date_str == 'today':
        target_date = now.date()
    elif date_str == 'yesterday':
        target_date = (now - timedelta(days=1)).date()
    else:
        try:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            return json.dumps({'error': f"Invalid date format: {date_str}. Use YYYY-MM-DD, 'today', or 'yesterday'."})
    day_start = datetime.combine(target_date, datetime.min.time(), tzinfo=TZ_LOCAL)
    day_end = datetime.combine(target_date, datetime.max.time(), tzinfo=TZ_LOCAL)
    start_ms = int(day_start.timestamp() * 1000)
    end_ms = int(day_end.timestamp() * 1000)
    journal_events = _load_jsonl_journal(target_date)
    journal_used = bool(journal_events)
    journal_events = [e for e in journal_events if start_ms <= int(float(e.get('timestamp', 0)) * 1000) <= end_ms]
    try:
        per_camera = {}
        all_events = []
        for cid in cam_ids:
            evt_key = _camera_key(_EVT_TMPL, cid)
            events_raw = ctx.r.xrange(evt_key, min=f'{start_ms}-0', max=f'{end_ms}-0')
            cam_events_by_id: dict[str, dict] = {}
            for msg_id, data in events_raw:
                evt = {k: v for k, v in data.items()}
                evt['event_id'] = msg_id
                evt['camera'] = cid
                if event_type and evt.get('event_type') != event_type:
                    continue
                if not _category_matches(evt.get('event_type', ''), category):
                    continue
                cam_events_by_id[msg_id] = evt
            for jevt in journal_events:
                if jevt.get('camera') != cid:
                    continue
                if event_type and jevt.get('event_type') != event_type:
                    continue
                if not _category_matches(jevt.get('event_type', ''), category):
                    continue
                jid = jevt.get('event_id') or ''
                if jid and jid not in cam_events_by_id:
                    cam_events_by_id[jid] = jevt
            cam_events = [cam_events_by_id[k] for k in sorted(cam_events_by_id.keys())]
            type_counts = {}
            identity_counts = {}
            for evt in cam_events:
                t = evt.get('event_type', 'unknown')
                type_counts[t] = type_counts.get(t, 0) + 1
                if t == 'person_identified':
                    name = evt.get('identity_name') or '<unknown>'
                    identity_counts[name] = identity_counts.get(name, 0) + 1
            per_camera[cid] = {'name': _camera_name(cid), 'total_events': len(cam_events), 'by_type': type_counts, 'by_identity': identity_counts}
            all_events.extend(cam_events)
        agg_type_counts = {}
        agg_identity_counts = {}
        for evt in all_events:
            t = evt.get('event_type', 'unknown')
            agg_type_counts[t] = agg_type_counts.get(t, 0) + 1
            if t == 'person_identified':
                name = evt.get('identity_name') or '<unknown>'
                agg_identity_counts[name] = agg_identity_counts.get(name, 0) + 1
        # "Detections" semantically means "things detected", not "events fired".
        # Each tracked person/vehicle produces a *_appeared (or *_detected) event
        # at session start and a *_left event at session end — so total_events
        # roughly doubles the actual count of distinct sightings. Surface a
        # `detection_count` that mirrors what a human means by "how many were
        # detected": one entry per primary appearance event.
        people_detected = agg_type_counts.get('person_appeared', 0)
        people_identified_total = agg_type_counts.get('person_identified', 0)
        people_identified_unique = len(agg_identity_counts)
        vehicles_detected = agg_type_counts.get('vehicle_detected', 0)
        vehicles_idle = agg_type_counts.get('vehicle_idle', 0)
        detection_count = people_detected + vehicles_detected

        # Build a one-line summary the LLM can copy verbatim — reduces the
        # chance of it picking the wrong number from the response. The model
        # has been consistently grabbing `total_events` for "how many
        # detections" questions despite the rules; pre-composed text is
        # harder to mis-quote than picking from a field list.
        if category == 'people' or (people_detected and not vehicles_detected):
            id_phrase = (
                f" · {people_identified_total} identifications across {people_identified_unique} unique people"
                if people_identified_total else ""
            )
            summary = (
                f"{people_detected} person detections (sessions){id_phrase}. "
                f"Raw event total: {len(all_events)} (includes person_left exit events)."
            )
        elif category == 'vehicles' or (vehicles_detected and not people_detected):
            idle_phrase = f" · {vehicles_idle} idling events" if vehicles_idle else ""
            summary = (
                f"{vehicles_detected} vehicle detections (sessions){idle_phrase}. "
                f"Raw event total: {len(all_events)} (includes vehicle_left exit events)."
            )
        else:
            summary = (
                f"{detection_count} detections (sessions): {people_detected} people, "
                f"{vehicles_detected} vehicles. Raw event total: {len(all_events)}."
            )

        result = {
            'date': str(target_date),
            'cameras_queried': cam_ids,
            # `summary` first — the LLM is biased toward the start of long JSON.
            'summary': summary,
            'how_to_answer': (
                "Use `summary` text when the user asks 'how many detections/people/vehicles'. "
                "Use `total_events` ONLY when the user explicitly says 'events' or 'activity'. "
                "Use `unique_people_identified` for 'how many people were identified'."
            ),
            'detection_count': detection_count,
            'total_events': len(all_events),
            'by_type': agg_type_counts,
            'by_identity': agg_identity_counts,
            'unique_people_identified': people_identified_unique,
            'latest_events': all_events[-10:] if len(all_events) > 10 else all_events,
            'journal_used': journal_used,
        }
        if len(cam_ids) > 1:
            result['per_camera'] = per_camera
        return json.dumps(result)
    except Exception as e:
        return json.dumps({'error': str(e)})
