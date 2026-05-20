"""
routes/ai_tools.py — AI tool definitions and executor functions.

PURPOSE:
    Defines the LLM tool/function calling schema (TOOLS list) and all
    tool executor functions. Each _tool_* function queries Redis, calls
    external APIs, or performs actions and returns a JSON string result
    that the LLM uses to formulate its response.

TOOLS (18):
    query_events, query_faces, send_telegram,
    schedule_reminder, get_system_status,
    get_live_scene, query_unknowns, query_events_by_date, query_zones,
    browse_vehicles, get_weather, query_event_patterns, capture_snapshot,
    capture_clip, query_notification_history, query_activity_heatmap,
    show_faces, analyze_image
"""

import collections
import os
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import routes as ctx
import routes.ai_state as ai_state

logger = logging.getLogger("dashboard.ai")
from contracts.tz import TZ_LOCAL  # validated single source of truth

# Every event_type string emitted somewhere in the pipeline. Used so type-aggregating
# tools can pre-populate zero counts — the LLM should never have to guess whether
# "no entry for vehicle_idle" means "zero occurred" vs "type doesn't exist."
KNOWN_EVENT_TYPES = (
    "person_appeared",
    "person_left",
    "person_identified",
    "vehicle_detected",
    "vehicle_left",
    "vehicle_idle",
    "face_enrolled",
    "face_reconciled",
    "action_changed",
    "unauthorized_access",
    # Stream-health events emitted by camera-ingester when frames stop/resume.
    "stream_stale",
    "stream_recovered",
    # Recorder health events emitted by recorder when ffmpeg keeps crashing.
    "recorder_error",
    "recorder_recovered",
)
KNOWN_EVENT_TYPES_DOC = ", ".join(KNOWN_EVENT_TYPES)

# Event categories — semantic buckets the LLM can filter by without needing
# to know every event_type string.
EVENT_CATEGORIES = {
    "people": ("person_appeared", "person_left", "person_identified"),
    "vehicles": ("vehicle_detected", "vehicle_left", "vehicle_idle"),
    "faces": ("face_enrolled", "face_reconciled", "person_identified"),
    "actions": ("action_changed",),
    "security": ("unauthorized_access",),
    "system": ("stream_stale", "stream_recovered", "recorder_error", "recorder_recovered"),
    "all": KNOWN_EVENT_TYPES,
}


def _category_matches(event_type: str, category: str) -> bool:
    """Check if an event_type belongs to a category. Empty/all => match everything."""
    if not category or category == "all":
        return True
    allowed = EVENT_CATEGORIES.get(category)
    if allowed is None:
        return True  # unknown category — don't filter (fail-open)
    return event_type in allowed


# ---------------------------------------------------------------------------
# Multi-camera helpers (Phase 9a)
# ---------------------------------------------------------------------------
# Tools that take a `camera` arg delegate to the shared resolver in cameras.py
# so registry semantics stay consistent across AI tools, Telegram, and routes.
#
# Conventions for the `camera` tool arg:
#   "all"          -> every registered camera, aggregated
#   "<id>"         -> just that one camera (must exist in cameras:registry)
#   missing/empty  -> the dashboard's primary camera (ctx.CAMERA_ID env)

import cameras as _camreg


def _get_camera_list() -> list:
    """Return all enabled cameras from the registry."""
    return _camreg.list_enabled_cameras()


def _resolve_camera(arg: str = "") -> list:
    """Resolve a tool's `camera` arg into a concrete list of camera ids.
    Returns [] iff a specific id was passed but doesn't exist."""
    return _camreg.resolve_camera_arg(arg, ctx.CAMERA_ID)


def _camera_key(template: str, camera_id: str, **extra) -> str:
    """Build a Redis key for any camera using contracts/streams.py templates.
    Lazy import to avoid circular issues at module load time."""
    from contracts.streams import stream_key
    return stream_key(template, camera_id=camera_id, **extra)


def _camera_name(camera_id: str) -> str:
    """Look up a camera's display name (falls back to id)."""
    return _camreg.camera_friendly_name(camera_id)


# Hash keys that should never end up in an LLM tool result. Anything
# matching one of these substrings (case-insensitive) gets replaced with
# "[redacted]" before the dict is JSON-serialized.
_REDACT_KEY_FRAGMENTS = ("password", "token", "secret", "rtsp", "url",
                          "api_key", "credential")


def _redact_sensitive(d: dict | None) -> dict:
    """Strip sensitive values from a Redis hash before showing to the LLM.

    The LLM's tool context flows into its reply, and the reply flows to
    Telegram, the browser, and chat history. We don't want RTSP URLs
    with `user:pass@` baked into any of those surfaces.
    """
    if not d:
        return {}
    out: dict = {}
    for k, v in d.items():
        kl = str(k).lower()
        if any(frag in kl for frag in _REDACT_KEY_FRAGMENTS):
            out[k] = "[redacted]"
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Tool definitions for the LLM
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_events",
            "description": "Search the most recent security events (newest first, max 50). **Defaults to camera='all' if not specified.** Returns events plus by_type and by_identity aggregations so you don't have to count manually. NOTE: only shows the latest events — use query_events_by_date for a full day's totals.",
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Number of recent events to return (max 50)",
                    },
                    "event_type": {
                        "type": "string",
                        "description": f"Filter by event type. Known types: {KNOWN_EVENT_TYPES_DOC}. Leave empty for all.",
                    },
                    "camera": {
                        "type": "string",
                        "description": "Camera id to query (e.g. 'cam1', 'cam2'), or 'all' for every camera. Default = primary camera.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_faces",
            "description": "List all enrolled/known faces in the system.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },

    {
        "type": "function",
        "function": {
            "name": "send_telegram",
            "description": "Send a message to the user via Telegram right now. Can include a live camera snapshot or a 5-second video clip. When the user asks for media from a specific camera, pass `camera`; otherwise defaults to primary.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "The message text to send",
                    },
                    "include_snapshot": {
                        "type": "boolean",
                        "description": "If true, attach the latest live camera frame to the message.",
                    },
                    "include_clip": {
                        "type": "boolean",
                        "description": "If true, capture and attach a 5-second video clip from the camera.",
                    },
                    "camera": {
                        "type": "string",
                        "description": "Camera id (e.g. 'cam1') for the snapshot/clip. Default = primary. Ignored for text-only messages.",
                    },
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_reminder",
            "description": "Schedule a reminder to be sent via Telegram at a specific time. Can include a snapshot or video clip captured at the scheduled time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "The reminder message",
                    },
                    "time_description": {
                        "type": "string",
                        "description": "When to send, e.g. '10:00 PM', 'in 30 minutes', '2026-02-21T22:00:00'",
                    },
                    "media_type": {
                        "type": "string",
                        "enum": ["text", "snapshot", "clip"],
                        "description": "Type of media to include: 'text' (default), 'snapshot' (camera photo), or 'clip' (5-second video).",
                    },
                },
                "required": ["message", "time_description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_system_status",
            "description": "Get current system status: stream sizes, config settings, notification preferences. Aggregates across all cameras by default; pass `camera` to scope to one.",
            "parameters": {
                "type": "object",
                "properties": {
                    "camera": {
                        "type": "string",
                        "description": "Camera id to scope status to (e.g. 'cam1', 'cam2'), or 'all'. Default = 'all' (aggregate across cameras).",
                    },
                },
                "required": [],
            },
        },
    },

    {
        "type": "function",
        "function": {
            "name": "get_live_scene",
            "description": "Get what's happening on camera RIGHT NOW — who is in frame, their actions, how long they've been there.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_unknowns",
            "description": "List unknown/unidentified faces that have been auto-captured by the system. Shows how many strangers have been seen.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_events_by_date",
            "description": "Query events filtered by date. **Defaults to camera='all', category='all'.** Use 'category' to filter — when user says 'only people / no vehicles / just faces', pass category='people'. Use 'event_type' to filter to ONE specific event type. Returns: total_events, by_type, by_identity, unique_people_identified, latest_events, per_camera with each camera's own by_type and by_identity. Use total_events for 'how many detections', by_identity for 'who was seen', per_camera.<cam>.by_identity for 'which camera saw which person'. NEVER invent identity counts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "Date to query in YYYY-MM-DD format. Use 'today' or 'yesterday' as shortcuts.",
                    },
                    "event_type": {
                        "type": "string",
                        "description": f"Optional: filter by ONE event type. Known types: {KNOWN_EVENT_TYPES_DOC}. Use 'category' instead if user said 'people' or 'vehicles' generally.",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["people", "vehicles", "faces", "actions", "security", "all"],
                        "description": "Filter by category. 'people' = person events only. 'vehicles' = vehicle events only. 'faces' = face events + person_identified. Default 'all'.",
                    },
                    "camera": {
                        "type": "string",
                        "description": "Camera id to query (e.g. 'cam1', 'cam2'), or 'all' for every camera. Default = primary camera.",
                    },
                },
                "required": ["date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_zones",
            "description": "List security zones defined on a camera, including their names, alert levels, and point coordinates. Zones are per-camera. Pass `camera` to pick one, or 'all' to list across every camera grouped by camera (default = primary).",
            "parameters": {
                "type": "object",
                "properties": {
                    "camera": {
                        "type": "string",
                        "description": "Camera id whose zones to list (e.g. 'cam1', 'cam2'), or 'all' for every camera grouped. Default = primary camera.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browse_vehicles",
            "description": "Browse vehicle detection snapshots for a given day. Shows snapshot images inline in the chat. Use when the user asks to see vehicle photos or detections. Vehicle detection currently only runs on cameras with `detect_vehicles=true` in the registry.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "Date in YYYY-MM-DD format, or 'today'/'yesterday'. Defaults to today.",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of recent snapshots to show (default 5, max 10)",
                    },
                    "camera": {
                        "type": "string",
                        "description": "Camera id to scope to (e.g. 'cam1'), or 'all'. Default = primary camera. Only cameras with vehicle detection enabled will have snapshots.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather conditions at the camera location. Useful for correlating activity with weather.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_event_patterns",
            "description": "Analyze event patterns and trends. **Defaults to camera='all', category='all'.** Use 'category' to filter — when user says 'only people / no vehicles / just faces', pass category='people' (person_appeared, person_left, person_identified) NOT category='all'. Hourly analysis returns: busiest_hour, top_hours (top 5 with full breakdown), active_window (first→last non-zero hour), hourly_breakdown (all 24 hrs), by_type_per_hour, by_identity_per_hour, per_camera_hourly. Use 'date' arg to scope to ONE day (today/yesterday/YYYY-MM-DD); use 'days_back' for rolling window. The 'scope' field in the response echoes the active filters back to you.",
            "parameters": {
                "type": "object",
                "properties": {
                    "analysis_type": {
                        "type": "string",
                        "enum": ["hourly", "daily", "type_breakdown"],
                        "description": "Type of analysis: 'hourly' (by hour of day), 'daily' (by day), 'type_breakdown' (events by type)",
                    },
                    "date": {
                        "type": "string",
                        "description": "Optional: scope to ONE day — 'today', 'yesterday', or YYYY-MM-DD. Overrides days_back if set.",
                    },
                    "days_back": {
                        "type": "integer",
                        "description": "How many days of history to analyze when 'date' is not set (default 7, max 30)",
                    },
                    "camera": {
                        "type": "string",
                        "description": "Camera id (e.g. 'cam1') or 'all' for every camera. Default = 'all'.",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["people", "vehicles", "faces", "actions", "security", "all"],
                        "description": "Filter events by category. 'people' = person events only (appearances/identifications/departures, NO faces or vehicles). 'vehicles' = vehicle events only. 'faces' = face enrollments + reconciliations + identifications. Default = 'all'.",
                    },
                },
                "required": ["analysis_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "capture_snapshot",
            "description": "Capture the current camera frame, show it in the chat, AND run the MiniCPM-V vision model on it to get a real visual description. Use this for ANY 'what do you see / what's happening / who is there / what are they doing' question — it both displays the image and tells you what's actually visible. Returns vision_analysis (visual description), context (weather + tracker state), source_camera. Pass `camera` to pick one (default = primary). Pass describe=false to skip vision (~3s faster) only if you just need the picture without a description.",
            "parameters": {
                "type": "object",
                "properties": {
                    "camera": {
                        "type": "string",
                        "description": "Camera id to capture (e.g. 'cam1', 'cam2'). Omit for the primary camera.",
                    },
                    "describe": {
                        "type": "boolean",
                        "description": "If true (default), also run MiniCPM-V on the frame and return its description in vision_analysis. Set false to skip for ~3s faster response when you only need the image.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "capture_clip",
            "description": "Record a 5-second LIVE video clip from a camera and show it in the chat. Use for 'show me what's happening right now'. For OLDER footage from past events (DVR), use find_dvr_segment instead — it returns a link to the right recording. Pass `camera` to pick one (default = primary).",
            "parameters": {
                "type": "object",
                "properties": {
                    "camera": {
                        "type": "string",
                        "description": "Camera id to record from (e.g. 'cam1', 'cam2'). Omit for the primary camera.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_dvr_segment",
            "description": "Find the DVR (.ts) recording segment that covers a given camera + date + time, and return a deep-link URL so the user can open it in the DVR tab. Use this when the user asks to see/review past footage (e.g. 'show me yesterday's busiest hour', 'I want to see the clip from 1pm'). DOES NOT extract or send video — returns a clickable URL to the existing DVR tab. Recommended workflow: (1) call query_event_patterns to find the busy hour, (2) call this with that hour as `time`, (3) format the response's deep_link as a markdown link for the user to click.",
            "parameters": {
                "type": "object",
                "properties": {
                    "camera": {
                        "type": "string",
                        "description": "Camera id (e.g. 'cam1'). Omit for primary camera. Must be a SINGLE camera, not 'all'.",
                    },
                    "date": {
                        "type": "string",
                        "description": "Date — 'today', 'yesterday', or YYYY-MM-DD. Default 'today'.",
                    },
                    "time": {
                        "type": "string",
                        "description": "Hour or time to find (e.g. '13:00', '1:00 PM', '17'). Omit to list all segments for that day.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_notification_history",
            "description": "Get recent Telegram notifications that were sent by the system. Returns notifications plus by_type and by_identity aggregations. Pass `camera` to scope to one camera or 'all' (default = 'all').",
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Number of recent notifications to return (default 20, max 50)",
                    },
                    "camera": {
                        "type": "string",
                        "description": "Camera id (e.g. 'cam1') or 'all'. Default = 'all'.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_activity_heatmap",
            "description": "Get a day-of-week × hour-of-day activity heatmap. Shows which days and hours are busiest, weekend vs weekday comparison, and peak activity windows. **Defaults to camera='all' if not specified.**",
            "parameters": {
                "type": "object",
                "properties": {
                    "days_back": {
                        "type": "integer",
                        "description": "How many days of history to analyze (default 14, max 30)",
                    },
                    "camera": {
                        "type": "string",
                        "description": "Camera id to analyze (e.g. 'cam1', 'cam2'), or 'all' for every camera. Default = primary camera.",
                    },
                },
                "required": [],
            },
        },
    },

    {
        "type": "function",
        "function": {
            "name": "show_faces",
            "description": "Show enrolled face photos. Sends up to 3 photos per person directly in the chat. Use when the user asks to see who is enrolled or wants to see face photos.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Optional: filter to a specific person's name. If omitted, shows all enrolled people.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_image",
            "description": "Analyze the current live camera frame using the MiniCPM-V vision model. Returns a detailed visual description of what the camera sees RIGHT NOW. Use this when the user asks 'what do you see', 'describe the scene', 'look at the camera', or similar requests that need actual visual understanding beyond the tracker metadata.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Optional: specific question or instruction for the vision model. E.g. 'describe any people in detail', 'what vehicles are parked', 'is the gate open or closed'. Defaults to a general scene description.",
                    },
                },
                "required": [],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Tool execution dispatcher
# ---------------------------------------------------------------------------
async def execute_tool(name: str, args: dict) -> str:
    """Execute a tool call and return the result as a string."""
    try:
        if name == "query_events":
            return _tool_query_events(args)
        elif name == "query_faces":
            return await _tool_query_faces()
        elif name == "send_telegram":
            return await _tool_send_telegram(args)
        elif name == "schedule_reminder":
            return _tool_schedule_reminder(args)
        elif name == "get_system_status":
            return _tool_get_system_status(args)
        elif name == "get_live_scene":
            return _tool_get_live_scene()
        elif name == "query_unknowns":
            return await _tool_query_unknowns()
        elif name == "query_events_by_date":
            return _tool_query_events_by_date(args)
        elif name == "query_zones":
            return _tool_query_zones(args)
        elif name == "browse_vehicles":
            return _tool_browse_vehicles(args)
        elif name == "get_weather":
            return await _tool_get_weather()
        elif name == "query_event_patterns":
            return _tool_query_event_patterns(args)
        elif name == "capture_snapshot":
            return await _tool_capture_snapshot(args)
        elif name == "capture_clip":
            return _tool_capture_clip(args)
        elif name == "query_notification_history":
            return _tool_query_notification_history(args)
        elif name == "query_activity_heatmap":
            return _tool_query_activity_heatmap(args)
        elif name == "show_faces":
            return await _tool_show_faces(args)
        elif name == "analyze_image":
            return await _tool_analyze_image(args)
        elif name == "find_dvr_segment":
            return _tool_find_dvr_segment(args)
        else:
            return json.dumps({"error": f"Unknown tool: {name}"})
    except Exception as e:
        logger.warning(f"Tool {name} error: {e}")
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------
def _tool_query_events(args: dict) -> str:
    """Query recent events from Redis. Defaults to ALL cameras when no camera arg
    is passed (analytical tool — cross-camera is the useful answer)."""
    from contracts.streams import EVENT_STREAM as _EVT_TMPL
    count = min(int(args.get("count", 20)), 50)
    event_type = args.get("event_type", "")
    camera_arg = args.get("camera", "all")

    cam_ids = _resolve_camera(camera_arg)
    if not cam_ids:
        return json.dumps({"error": f"Unknown camera id: '{camera_arg}'", "available": [c["id"] for c in _get_camera_list()]})

    try:
        all_events = []
        for cid in cam_ids:
            stream = _camera_key(_EVT_TMPL, cid)
            events_raw = ctx.r.xrevrange(stream, count=count)
            for msg_id, data in events_raw:
                evt = {k: v for k, v in data.items()}
                evt["event_id"] = msg_id
                evt["camera_id"] = cid
                evt["camera_name"] = _camera_name(cid)
                if event_type and evt.get("event_type") != event_type:
                    continue
                all_events.append(evt)
        # Sort newest first across cameras, then cap to `count`
        all_events.sort(key=lambda e: e.get("event_id", ""), reverse=True)
        capped = all_events[:count]
        # Aggregate so the LLM doesn't hallucinate breakdowns from the events list
        by_type = {}
        by_identity = {}
        for evt in capped:
            t = evt.get("event_type", "unknown")
            by_type[t] = by_type.get(t, 0) + 1
            if t == "person_identified":
                name = evt.get("identity_name") or "<unknown>"
                by_identity[name] = by_identity.get(name, 0) + 1
        return json.dumps({
            "events": capped,
            "showing_count": len(capped),
            "limit_requested": count,
            "by_type": by_type,
            "by_identity": by_identity,
            "unique_people_identified": len(by_identity),
            "cameras_queried": cam_ids,
            "note": "This shows only the most recent events (max 50). Use query_events_by_date for a full day's totals.",
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_get_live_scene() -> str:
    """Aggregate the current scene from every registered camera.

    Always multi-camera-aware — no `camera` arg. The LLM gets a per-camera
    breakdown ('Front: 2 people · Basement: 0') so it can pick which one to
    talk about without needing to call this tool multiple times.
    """
    from contracts.streams import (
        STATE_KEY as _STATE_TMPL,
        IDENTITY_KEY as _IDKEY_TMPL,
    )
    cam_ids = _resolve_camera("all")
    if not cam_ids:
        # Fallback: at least show the primary camera even if registry is empty
        cam_ids = [ctx.CAMERA_ID]

    cameras_data = []
    total_people = 0
    for cid in cam_ids:
        state_key = _camera_key(_STATE_TMPL, cid)
        id_key = _camera_key(_IDKEY_TMPL, cid)
        cam_block = {"id": cid, "name": _camera_name(cid)}
        try:
            state = ctx.r.hgetall(state_key)
            if state:
                n = int(state.get("num_people", "0"))
                cam_block["num_people"] = n
                total_people += n
                # Decode person list if present
                try:
                    persons = json.loads(state.get("people", "[]"))
                    if persons:
                        cam_block["persons"] = persons
                except (json.JSONDecodeError, TypeError):
                    pass
            else:
                cam_block["num_people"] = 0
            # Add identity overlay if face-recognizer wrote one
            id_state = ctx.r.hgetall(id_key)
            if id_state and id_state.get("identities"):
                try:
                    cam_block["identities"] = json.loads(id_state["identities"])
                except (json.JSONDecodeError, TypeError):
                    pass
        except Exception as e:
            cam_block["error"] = str(e)
        cameras_data.append(cam_block)

    if not cameras_data:
        return json.dumps({"scene": "No camera data available — registry empty or tracker not running."})

    # Aggregate identified people across every camera so the LLM can answer
    # "who's here right now" without having to scan per-camera blocks itself.
    identified_set = set()
    for cb in cameras_data:
        for ident in cb.get("identities", []) or []:
            if isinstance(ident, dict):
                name = ident.get("name") or ident.get("identity_name")
                if name and name != "unknown":
                    identified_set.add(name)
    identified_people = sorted(identified_set)

    # Preserved single-camera shape for backward compat with prompts that may
    # assume `num_people`/`persons` at top level — populate from the primary cam.
    primary_block = next((c for c in cameras_data if c["id"] == ctx.CAMERA_ID), cameras_data[0])
    out = {
        "cameras": cameras_data,
        "total_people_across_cameras": total_people,
        "identified_people_now": identified_people,
        "identified_people_count": len(identified_people),
        "num_people": primary_block.get("num_people", 0),
    }
    if "persons" in primary_block:
        out["persons"] = primary_block["persons"]

    return json.dumps(out)


async def _tool_query_unknowns() -> str:
    """Query unknown/auto-captured faces from the face recognizer."""
    import httpx
    try:
        async with httpx.AsyncClient() as client:
            # Query BOTH endpoints: unknowns (auto-captured) and faces (enrolled)
            unknowns_resp = await client.get(f"{ctx.FACE_API_URL}/api/unknowns", timeout=5)
            faces_resp = await client.get(f"{ctx.FACE_API_URL}/api/faces", timeout=5)

        unknowns = []
        if unknowns_resp.status_code == 200:
            data = unknowns_resp.json()
            unknowns = data.get("unknowns", data) if isinstance(data, dict) else data
            if not isinstance(unknowns, list):
                unknowns = []

        enrolled_names = []
        if faces_resp.status_code == 200:
            data = faces_resp.json()
            face_list = data.get("faces", data) if isinstance(data, dict) else data
            if isinstance(face_list, list):
                enrolled_names = [f.get("name", "?") for f in face_list if isinstance(f, dict)]

        # Dedupe enrolled names — face-recognizer returns one row per photo
        unique_enrolled = sorted(set(enrolled_names))
        SHOW_LIMIT = 20
        shown = unknowns[:SHOW_LIMIT]
        return json.dumps({
            "enrolled_people_count": len(unique_enrolled),
            "enrolled_names": unique_enrolled,
            "enrolled_photo_count": len(enrolled_names),
            "unknown_count": len(unknowns),
            "unknowns_shown": len(shown),
            "truncated": len(unknowns) > SHOW_LIMIT,
            "unknowns": [{"id": f.get("id", "?"), "first_seen": f.get("first_seen", "?")} for f in shown],
            "note": (
                f"Showing latest {len(shown)} of {len(unknowns)} unknown faces."
                if len(unknowns) > SHOW_LIMIT
                else f"All {len(unknowns)} unknown faces shown."
            ),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


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
    import os as _os
    journal_path = f"/data/events/{target_date}.jsonl"
    if not _os.path.isfile(journal_path):
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
                # Restore timestamp as a string for downstream compatibility
                ts = evt.get("timestamp")
                if ts is not None:
                    evt["timestamp"] = str(ts)
                out.append(evt)
    except Exception:
        return []
    return out


def _tool_query_events_by_date(args: dict) -> str:
    """Query events filtered by date. Multi-camera aware — defaults to ALL cameras
    when no camera is specified (analytical tool; cross-camera is the useful answer).

    Merges the Redis events stream with the JSONL journal at
    /data/events/<date>.jsonl so requests for past dates still return data
    even if the Redis stream has trimmed those events (default cap 5000 per
    camera). Dedup is by event_id.
    """
    from contracts.streams import EVENT_STREAM as _EVT_TMPL

    date_str = args.get("date", "today")
    event_type = args.get("event_type", "")
    camera_arg = args.get("camera", "all")
    category = (args.get("category") or "all").strip().lower()
    if category not in EVENT_CATEGORIES:
        return json.dumps({
            "error": f"Unknown category '{category}'. Valid: {list(EVENT_CATEGORIES.keys())}",
        })

    cam_ids = _resolve_camera(camera_arg)
    if not cam_ids:
        return json.dumps({
            "error": f"Unknown camera '{camera_arg}'. Use 'all' or a registered camera id.",
            "available": [c["id"] for c in _get_camera_list()],
        })

    # Parse date (use local timezone so "today" = EST, not UTC in Docker)
    now = datetime.now(TZ_LOCAL)
    if date_str == "today":
        target_date = now.date()
    elif date_str == "yesterday":
        target_date = (now - timedelta(days=1)).date()
    else:
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return json.dumps({"error": f"Invalid date format: {date_str}. Use YYYY-MM-DD, 'today', or 'yesterday'."})

    # Convert date to Redis stream timestamp range (timezone-aware)
    day_start = datetime.combine(target_date, datetime.min.time(), tzinfo=TZ_LOCAL)
    day_end = datetime.combine(target_date, datetime.max.time(), tzinfo=TZ_LOCAL)
    start_ms = int(day_start.timestamp() * 1000)
    end_ms = int(day_end.timestamp() * 1000)

    # Load the on-disk JSONL journal once (it's per-date, not per-camera —
    # contains events from every camera for that day). We'll filter per
    # camera in the loop below.
    journal_events = _load_jsonl_journal(target_date)
    journal_used = bool(journal_events)
    # Sanity-trim journal entries to the requested ms window — guards against
    # a clock skew between writer and reader, and ignores stragglers.
    journal_events = [
        e for e in journal_events
        if start_ms <= int(float(e.get("timestamp", 0)) * 1000) <= end_ms
    ]

    try:
        per_camera = {}
        all_events = []
        for cid in cam_ids:
            evt_key = _camera_key(_EVT_TMPL, cid)
            events_raw = ctx.r.xrange(evt_key, min=f"{start_ms}-0", max=f"{end_ms}-0")
            cam_events_by_id: dict[str, dict] = {}
            for msg_id, data in events_raw:
                evt = {k: v for k, v in data.items()}
                evt["event_id"] = msg_id
                evt["camera"] = cid
                if event_type and evt.get("event_type") != event_type:
                    continue
                if not _category_matches(evt.get("event_type", ""), category):
                    continue
                cam_events_by_id[msg_id] = evt
            # Merge JSONL events for this camera. Dedup by event_id — Redis
            # entries win when both sources have the same id.
            for jevt in journal_events:
                if jevt.get("camera") != cid:
                    continue
                if event_type and jevt.get("event_type") != event_type:
                    continue
                if not _category_matches(jevt.get("event_type", ""), category):
                    continue
                jid = jevt.get("event_id") or ""
                if jid and jid not in cam_events_by_id:
                    cam_events_by_id[jid] = jevt
            # Sort by event_id (stream id == ms timestamp, so this is chronological)
            cam_events = [cam_events_by_id[k] for k in sorted(cam_events_by_id.keys())]
            type_counts = {}
            identity_counts = {}
            for evt in cam_events:
                t = evt.get("event_type", "unknown")
                type_counts[t] = type_counts.get(t, 0) + 1
                if t == "person_identified":
                    name = evt.get("identity_name") or "<unknown>"
                    identity_counts[name] = identity_counts.get(name, 0) + 1
            per_camera[cid] = {
                "name": _camera_name(cid),
                "total_events": len(cam_events),
                "by_type": type_counts,
                "by_identity": identity_counts,
            }
            all_events.extend(cam_events)

        # Aggregate totals
        agg_type_counts = {}
        agg_identity_counts = {}
        for evt in all_events:
            t = evt.get("event_type", "unknown")
            agg_type_counts[t] = agg_type_counts.get(t, 0) + 1
            if t == "person_identified":
                name = evt.get("identity_name") or "<unknown>"
                agg_identity_counts[name] = agg_identity_counts.get(name, 0) + 1

        result = {
            "date": str(target_date),
            "cameras_queried": cam_ids,
            "total_events": len(all_events),
            "by_type": agg_type_counts,
            "by_identity": agg_identity_counts,
            "unique_people_identified": len(agg_identity_counts),
            "latest_events": all_events[-10:] if len(all_events) > 10 else all_events,
            "journal_used": journal_used,  # True if /data/events/<date>.jsonl contributed
        }
        if len(cam_ids) > 1:
            result["per_camera"] = per_camera
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_query_zones(args: dict = None) -> str:
    """List security zones defined per camera. Multi-camera aware."""
    from contracts.streams import ZONE_KEY as _ZONE_TMPL

    args = args or {}
    camera_arg = args.get("camera", "")
    cam_ids = _resolve_camera(camera_arg)
    if not cam_ids:
        return json.dumps({
            "error": f"Unknown camera '{camera_arg}'. Use 'all' or a registered camera id.",
            "available": [c["id"] for c in _get_camera_list()],
        })

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
                    zone["id"] = zone_id
                    zones.append(zone)
                except (json.JSONDecodeError, TypeError):
                    zones.append({"id": zone_id, "raw": zone_json})
            per_camera[cid] = {
                "name": _camera_name(cid),
                "zones": zones,
                "count": len(zones),
            }
            total += len(zones)

        # If single camera, return the flat shape for backward compat
        if len(cam_ids) == 1:
            cid = cam_ids[0]
            entry = per_camera[cid]
            return json.dumps({
                "camera": cid,
                "camera_name": entry["name"],
                "zones": entry["zones"],
                "count": entry["count"],
                "message": "No zones defined yet." if entry["count"] == 0 else None,
            })
        return json.dumps({
            "cameras_queried": cam_ids,
            "total_zones": total,
            "per_camera": per_camera,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_browse_vehicles(args: dict) -> str:
    """List vehicle detection snapshots for a given day and stash images for display.

    Multi-camera note: vehicle snapshots are currently saved to a shared
    directory (`VEHICLE_SNAPSHOT_DIR/{date}/`), not per-camera. If a `camera`
    arg is passed, we validate it exists and that it has detect_vehicles=true
    in the registry. If detect_vehicles is false, return an empty result with
    an explanation rather than misleading snapshots from another camera.
    """
    import glob

    date_str = args.get("date", "today")
    count_requested = min(int(args.get("count", 5)), 10)  # Max 10 images
    camera_arg = args.get("camera", "")

    cam_ids = _resolve_camera(camera_arg)
    if not cam_ids:
        return json.dumps({
            "error": f"Unknown camera '{camera_arg}'. Use 'all' or a registered camera id.",
            "available": [c["id"] for c in _get_camera_list()],
        })

    # Check which of the requested cameras actually have vehicle detection on
    cams_with_vehicles = []
    cams_without_vehicles = []
    for c in _get_camera_list():
        if c["id"] in cam_ids:
            if c.get("detect_vehicles", True):
                cams_with_vehicles.append(c["id"])
            else:
                cams_without_vehicles.append(c["id"])

    # If user asked for a single camera and it has no vehicle detection, bail early.
    if camera_arg and camera_arg.lower() != "all" and not cams_with_vehicles:
        return json.dumps({
            "date": "n/a",
            "count": 0,
            "snapshots": [],
            "camera": camera_arg,
            "message": f"Camera '{camera_arg}' does not have vehicle detection enabled (detect_vehicles=false). No vehicle snapshots will exist for it.",
        })

    now = datetime.now(TZ_LOCAL)
    if date_str == "today":
        target_date = now.strftime("%Y-%m-%d")
    elif date_str == "yesterday":
        target_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        target_date = date_str

    # Date arg is interpolated into filesystem paths (`{snapshot_dir}/
    # {cam}/{date}`) and into URLs the chat UI renders. A prompt-injected
    # `date="../../etc"` would otherwise let the LLM enumerate paths.
    # Force the date to YYYY-MM-DD before any path operation.
    import re as _re
    if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", target_date):
        return json.dumps({
            "error": f"Invalid date format '{target_date}'. Use YYYY-MM-DD or 'today'/'yesterday'.",
        })

    snapshot_dir = ctx.VEHICLE_SNAPSHOT_DIR or "/data/snapshots/vehicles"

    # Build per-camera + legacy day-dir list. Per-camera path takes precedence;
    # legacy /data/snapshots/vehicles/{date}/ is walked too for old data.
    candidate_dirs: list[tuple[str, str]] = []  # (camera_id or "", path)
    for cid in cams_with_vehicles:
        p = os.path.join(snapshot_dir, cid, target_date)
        if os.path.isdir(p):
            candidate_dirs.append((cid, p))
    # Legacy root (only include if camera_arg is unset or "all")
    if not camera_arg or camera_arg.lower() == "all":
        legacy = os.path.join(snapshot_dir, target_date)
        if os.path.isdir(legacy):
            candidate_dirs.append(("", legacy))

    try:
        if not candidate_dirs:
            return json.dumps({"date": target_date, "count": 0, "snapshots": [], "message": f"No vehicle snapshots for {target_date}"})

        snapshots = []
        for src_cam, day_dir in candidate_dirs:
            files = sorted(glob.glob(os.path.join(day_dir, "*.jpg")))
            for f in files:
                basename = os.path.basename(f)
                base_name = basename.rsplit(".", 1)[0]
                parts = base_name.split("_", 1)
                time_str = parts[0].replace("-", ":") if parts else ""
                vehicle_class = parts[1] if len(parts) > 1 else "vehicle"
                cam_segment = src_cam if src_cam else "_legacy"
                snapshots.append({
                    "filename": basename,
                    "time": time_str,
                    "vehicle_class": vehicle_class,
                    "camera": src_cam,
                    "size_kb": round(os.path.getsize(f) / 1024, 1),
                    "url": f"/api/browse/snapshot/{cam_segment}/{target_date}/{basename}",
                })
        # Sort by time across cameras
        snapshots.sort(key=lambda s: s.get("time", ""))

        # Stash the last N images for inline display in the chat
        display_snapshots = snapshots[-count_requested:]
        if display_snapshots:
            ai_state.stash_images([
                {"url": s["url"], "caption": f"{s['time']} — {s['vehicle_class']}"}
                for s in display_snapshots
            ])

        # Build a storage-layout note so the LLM understands the camera scoping limitation
        layout_note = None
        if cams_without_vehicles:
            layout_note = (
                f"Note: snapshots are saved to a shared directory, not per-camera. "
                f"Cameras {cams_without_vehicles} have vehicle detection disabled and produce no snapshots. "
                f"Cameras producing snapshots: {cams_with_vehicles}."
            )

        return json.dumps({
            "date": target_date,
            "cameras_requested": cam_ids,
            "cameras_with_vehicle_detection": cams_with_vehicles,
            "count": len(snapshots),
            "snapshots": snapshots[-20:],  # Last 20 metadata
            "images_will_be_shown": len(display_snapshots),
            "note": f"Showing last {min(20, len(snapshots))} of {len(snapshots)}" if len(snapshots) > 20 else None,
            "layout_note": layout_note,
            "instruction": "The vehicle snapshot images will be displayed inline to the user automatically. Describe the snapshots using the metadata (timestamps, vehicle classes). Do NOT try to embed the images yourself.",
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


async def _tool_get_weather() -> str:
    """Get current weather from OpenWeatherMap."""
    import httpx
    api_key = os.getenv("OPENWEATHER_API_KEY", "")
    lat = os.getenv("LOCATION_LAT", "")
    lon = os.getenv("LOCATION_LON", "")

    if not api_key:
        return json.dumps({"error": "OPENWEATHER_API_KEY not configured"})
    if not lat or not lon:
        return json.dumps({"error": "LOCATION_LAT/LON not configured"})

    try:
        url = f"https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={api_key}&units=metric"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            weather = {
                "condition": data.get("weather", [{}])[0].get("description", "unknown"),
                "temperature_c": data.get("main", {}).get("temp"),
                "feels_like_c": data.get("main", {}).get("feels_like"),
                "humidity_pct": data.get("main", {}).get("humidity"),
                "wind_speed_ms": data.get("wind", {}).get("speed"),
                "visibility_m": data.get("visibility"),
                "sunrise": data.get("sys", {}).get("sunrise"),
                "sunset": data.get("sys", {}).get("sunset"),
            }
            return json.dumps(weather)
        return json.dumps({"error": f"Weather API returned {resp.status_code}"})
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_query_event_patterns(args: dict) -> str:
    """Analyze event patterns for trends. Multi-camera aware (aggregates across requested cameras)."""
    from collections import defaultdict
    from contracts.streams import EVENT_STREAM as _EVT_TMPL

    analysis_type = args.get("analysis_type", "hourly")
    days_back = min(int(args.get("days_back", 7)), 30)
    # Analytical tool — default to all cameras when omitted
    camera_arg = args.get("camera", "all")
    # Optional: scope to a single calendar day (overrides days_back)
    date_str = args.get("date", "").strip()
    # Optional: filter to a category (people / vehicles / faces / actions / security / all)
    category = (args.get("category") or "all").strip().lower()
    if category not in EVENT_CATEGORIES:
        return json.dumps({
            "error": f"Unknown category '{category}'. Valid: {list(EVENT_CATEGORIES.keys())}",
        })

    cam_ids = _resolve_camera(camera_arg)
    if not cam_ids:
        return json.dumps({
            "error": f"Unknown camera '{camera_arg}'. Use 'all' or a registered camera id.",
            "available": [c["id"] for c in _get_camera_list()],
        })

    # Calculate time range (timezone-aware for correct local day boundaries)
    now = datetime.now(TZ_LOCAL)
    if date_str:
        # Single-day scope — use local midnight boundaries
        if date_str == "today":
            target_date = now.date()
        elif date_str == "yesterday":
            target_date = (now - timedelta(days=1)).date()
        else:
            try:
                target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                return json.dumps({"error": f"Invalid date '{date_str}'. Use YYYY-MM-DD, 'today', or 'yesterday'."})
        day_start = datetime.combine(target_date, datetime.min.time(), tzinfo=TZ_LOCAL)
        day_end = datetime.combine(target_date, datetime.max.time(), tzinfo=TZ_LOCAL)
        start_ms = int(day_start.timestamp() * 1000)
        end_ms = int(day_end.timestamp() * 1000)
        scope_label = f"date={target_date}"
    else:
        start_date = now - timedelta(days=days_back)
        start_ms = int(start_date.timestamp() * 1000)
        end_ms = int(now.timestamp() * 1000)
        scope_label = f"last {days_back} days"
    if category != "all":
        scope_label += f" · category={category} (event_types={list(EVENT_CATEGORIES[category])})"

    try:
        # Aggregate events across all requested cameras, but also keep per-camera
        # buckets so we can return per_camera_hourly etc.
        # Apply category filter at fetch time so all downstream aggregations
        # automatically respect it.
        events_raw = []
        events_per_cam: dict[str, list] = {}
        for cid in cam_ids:
            evt_key = _camera_key(_EVT_TMPL, cid)
            cam_evts_all = ctx.r.xrange(evt_key, min=f"{start_ms}-0", max=f"{end_ms}-0")
            if category != "all":
                cam_evts = [
                    (mid, data) for mid, data in cam_evts_all
                    if _category_matches(data.get("event_type", ""), category)
                ]
            else:
                cam_evts = cam_evts_all
            events_per_cam[cid] = cam_evts
            events_raw.extend(cam_evts)

        if analysis_type == "hourly":
            hourly = defaultdict(int)
            hourly_by_type = defaultdict(lambda: defaultdict(int))
            hourly_by_identity = defaultdict(lambda: defaultdict(int))
            hourly_per_cam = {cid: defaultdict(int) for cid in cam_ids}

            def _parse_hour(ts_raw):
                ts_str = str(ts_raw)
                try:
                    if "." in ts_str:
                        return datetime.fromtimestamp(float(ts_str), tz=TZ_LOCAL).hour
                    return datetime.fromisoformat(ts_str).hour
                except (ValueError, TypeError, OSError):
                    return None

            for cid, cam_evts in events_per_cam.items():
                for msg_id, data in cam_evts:
                    hour = _parse_hour(data.get("timestamp") or data.get("first_seen", ""))
                    if hour is None:
                        continue
                    hourly[hour] += 1
                    hourly_per_cam[cid][hour] += 1
                    etype = data.get("event_type", "unknown")
                    hourly_by_type[hour][etype] += 1
                    if etype == "person_identified":
                        name = data.get("identity_name") or "<unknown>"
                        hourly_by_identity[hour][name] += 1

            # Always emit all 24 hours so quiet hours are explicit (0 vs missing)
            hourly_breakdown = {f"{h:02d}:00": hourly.get(h, 0) for h in range(24)}
            by_type_per_hour = {f"{h:02d}:00": dict(hourly_by_type.get(h, {})) for h in range(24)}
            by_identity_per_hour = {
                f"{h:02d}:00": dict(hourly_by_identity.get(h, {})) for h in range(24)
            }
            per_camera_hourly = {
                cid: {f"{h:02d}:00": hourly_per_cam[cid].get(h, 0) for h in range(24)}
                for cid in cam_ids
            }

            # Top 5 busiest hours (sorted desc), plus active window
            ranked = sorted(hourly.items(), key=lambda x: x[1], reverse=True)
            top_hours = [
                {
                    "hour": f"{h:02d}:00",
                    "count": cnt,
                    "by_type": dict(hourly_by_type.get(h, {})),
                    "by_identity": dict(hourly_by_identity.get(h, {})),
                    "per_camera": {cid: hourly_per_cam[cid].get(h, 0) for cid in cam_ids},
                }
                for h, cnt in ranked[:5] if cnt > 0
            ]
            active_hours = sorted([h for h, c in hourly.items() if c > 0])
            if active_hours:
                active_window = f"{active_hours[0]:02d}:00–{active_hours[-1]:02d}:00"
                quiet_hours = [f"{h:02d}:00" for h in range(24) if hourly.get(h, 0) == 0]
            else:
                active_window = "no activity"
                quiet_hours = [f"{h:02d}:00" for h in range(24)]

            busiest = ranked[0] if ranked and ranked[0][1] > 0 else (0, 0)
            return json.dumps({
                "analysis": "hourly",
                "scope": scope_label,
                "cameras_queried": cam_ids,
                "days_analyzed": days_back if not date_str else 1,
                "total_events": len(events_raw),
                "busiest_hour": f"{busiest[0]:02d}:00 ({busiest[1]} events)",
                "top_hours": top_hours,
                "active_window": active_window,
                "quiet_hours_count": len(quiet_hours),
                "hourly_breakdown": hourly_breakdown,
                "by_type_per_hour": by_type_per_hour,
                "by_identity_per_hour": by_identity_per_hour,
                "per_camera_hourly": per_camera_hourly,
            })

        elif analysis_type == "daily":
            daily = defaultdict(int)
            daily_by_type = defaultdict(lambda: defaultdict(int))
            for msg_id, data in events_raw:
                ts = data.get("timestamp") or data.get("first_seen", "")
                try:
                    if "." in str(ts):
                        dt = datetime.fromtimestamp(float(ts), tz=TZ_LOCAL)
                    else:
                        dt = datetime.fromisoformat(str(ts))
                    day_key = dt.strftime("%Y-%m-%d")
                    daily[day_key] += 1
                    daily_by_type[day_key][data.get("event_type", "unknown")] += 1
                except (ValueError, TypeError, OSError):
                    continue

            avg = sum(daily.values()) / max(len(daily), 1)
            return json.dumps({
                "analysis": "daily",
                "cameras_queried": cam_ids,
                "days_analyzed": days_back,
                "total_events": len(events_raw),
                "daily_breakdown": dict(sorted(daily.items())),
                "by_type_per_day": {d: dict(daily_by_type[d]) for d in sorted(daily_by_type.keys())},
                "daily_average": round(avg, 1),
                "busiest_day": max(daily.items(), key=lambda x: x[1])[0] if daily else "none",
            })

        elif analysis_type == "type_breakdown":
            # Pre-seed all known types so the LLM sees "0" instead of missing keys
            types = {t: 0 for t in KNOWN_EVENT_TYPES}
            for msg_id, data in events_raw:
                evt_type = data.get("event_type", "unknown")
                types[evt_type] = types.get(evt_type, 0) + 1

            return json.dumps({
                "analysis": "type_breakdown",
                "cameras_queried": cam_ids,
                "days_analyzed": days_back,
                "total_events": len(events_raw),
                "by_type": dict(sorted(types.items(), key=lambda x: x[1], reverse=True)),
            })

        else:
            return json.dumps({"error": f"Unknown analysis type: {analysis_type}"})
    except Exception as e:
        return json.dumps({"error": str(e)})


async def _tool_capture_snapshot(args: dict = None) -> str:
    """Capture camera frame WITH automatic visual analysis from MiniCPM-V.

    Returns: snapshot shown to user, tracker state context, AND a free-text
    visual description from the vision model so the chat LLM doesn't have to
    describe blindly. Vision pass runs by default — pass describe=false only
    if you want a raw snapshot without the ~3s vision inference.
    """
    args = args or {}
    import base64
    import httpx
    from routes.notifications import get_latest_frame, describe_scene

    cam_ids = _resolve_camera(args.get("camera", ""))
    if not cam_ids:
        return json.dumps({"error": f"Unknown camera id: '{args.get('camera')}'", "available": [c["id"] for c in _get_camera_list()]})
    describe = args.get("describe", True)
    if isinstance(describe, str):
        describe = describe.lower() not in ("false", "0", "no")
    # capture_snapshot picks one camera at a time even if user said "all" —
    # snapshot is a single image. If "all" was passed, use primary.
    snap_camera = cam_ids[0] if len(cam_ids) == 1 else ctx.CAMERA_ID

    try:
        frame = get_latest_frame(camera_id=snap_camera)
        if not frame:
            return json.dumps({"error": f"No frame available for camera '{snap_camera}' — may be offline"})

        b64 = base64.b64encode(frame).decode("utf-8")

        # Stash the base64 for the chat handler — NOT for the LLM
        ai_state.stash_snapshot(b64)

        # Gather contextual data so the AI can describe the scene intelligently
        # Always record the source camera — when user said "all" we silently picked one,
        # and the LLM needs to caption the response correctly.
        context = {
            "source_camera_id": snap_camera,
            "source_camera_name": _camera_name(snap_camera),
        }

        # Weather from conditions endpoint cache or direct fetch
        try:
            api_key = os.getenv("OPENWEATHER_API_KEY", "")
            lat = os.getenv("LOCATION_LAT", "")
            lon = os.getenv("LOCATION_LON", "")
            if api_key and lat and lon:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        "https://api.openweathermap.org/data/2.5/weather",
                        params={"lat": lat, "lon": lon, "appid": api_key, "units": "metric"},
                        timeout=3,
                    )
                if resp.status_code == 200:
                    w = resp.json()
                    context["weather"] = {
                        "temp_c": round(w["main"]["temp"]),
                        "feels_like_c": round(w["main"]["feels_like"]),
                        "description": w["weather"][0]["description"],
                        "humidity": w["main"]["humidity"],
                        "wind_kmh": round(w["wind"]["speed"] * 3.6),
                    }
        except Exception:
            pass

        # Current scene state — use the per-camera state key so a
        # snapshot of cam2 returns cam2's scene, not the primary's.
        try:
            from contracts.streams import STATE_KEY as _STATE_TMPL
            state_key = _camera_key(_STATE_TMPL, snap_camera)
            state = ctx.r.hgetall(state_key)
            if state:
                context["scene"] = {
                    "people_in_frame": int(state.get("num_people", 0)),
                }
                persons_raw = state.get("people", "[]")
                try:
                    persons = json.loads(persons_raw)
                    if persons:
                        context["scene"]["persons"] = persons
                except (json.JSONDecodeError, TypeError):
                    pass
        except Exception:
            pass

        # Current time
        now = datetime.now(TZ_LOCAL)
        context["timestamp"] = now.strftime("%I:%M %p, %B %d %Y")
        context["time_period"] = "night" if now.hour < 6 or now.hour >= 21 else "day" if 8 <= now.hour < 18 else "twilight"

        # Run MiniCPM-V on the frame so the chat model gets a real visual
        # description, not just tracker-state metadata. ~3s overhead on a
        # cold model load; ~1s when warm. Caller can disable via describe=false.
        visual_description = ""
        if describe:
            try:
                visual_description = await describe_scene(
                    frame,
                    prompt=(
                        "Describe what you see in this security camera image. "
                        "Mention people, vehicles, activity, lighting, and anything notable. "
                        "Be concise — 2-3 sentences."
                    ),
                    timeout=30.0,
                ) or ""
            except Exception as e:
                logger.warning(f"capture_snapshot vision pass failed: {e}")

        # Return ONLY the small metadata + description text to the LLM — no base64!
        out = {
            "snapshot_captured": True,
            "size_kb": round(len(frame) / 1024, 1),
            "context": context,
            "instruction": (
                "A live camera snapshot has been captured and will be shown to the user "
                "automatically. The 'vision_analysis' field (if present) is a description "
                "from the MiniCPM-V vision model of what the camera actually sees — use it "
                "to answer the user's question instead of guessing from tracker state. "
                "Do NOT output base64 or reference the image data directly."
            ),
        }
        if visual_description:
            out["vision_analysis"] = visual_description
        elif describe:
            out["vision_analysis"] = "(vision model unavailable or timed out)"
        return json.dumps(out)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_capture_clip(args: dict = None) -> str:
    """Capture 5-second MP4 clip from a live camera. Multi-camera aware."""
    from routes.notifications import build_clip
    from contracts.streams import STATE_KEY as _STATE_TMPL
    import uuid as _uuid

    args = args or {}
    camera_arg = args.get("camera", "")
    cam_ids = _resolve_camera(camera_arg)
    if not cam_ids:
        return json.dumps({
            "error": f"Unknown camera '{camera_arg}'. Use 'all' or a registered camera id.",
            "available": [c["id"] for c in _get_camera_list()],
        })
    # Clips are point-in-time recordings — for "all" we just pick the first
    # camera (LLM should ask explicitly which camera if it wants others).
    target_cam = cam_ids[0]

    try:
        mp4_bytes = build_clip(duration=5.0, fps=10, camera_id=target_cam)
        if not mp4_bytes:
            return json.dumps({"error": f"Clip capture failed on camera '{target_cam}' — may be offline or not enough frames"})

        # Save raw OpenCV clip to temp file first
        clip_dir = os.path.join("/data/snapshots", "clips")
        os.makedirs(clip_dir, exist_ok=True)
        filename = f"{datetime.now(TZ_LOCAL).strftime('%Y%m%d_%H%M%S')}_{target_cam}_{_uuid.uuid4().hex[:6]}.mp4"
        filepath = os.path.join(clip_dir, filename)
        raw_path = filepath + ".raw.mp4"
        with open(raw_path, "wb") as f:
            f.write(mp4_bytes)

        # Re-encode to H.264 for browser playback
        # OpenCV's mp4v (MPEG-4 Part 2) isn't browser-compatible
        import subprocess
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", raw_path,
                 "-c:v", "libx264", "-preset", "ultrafast",
                 "-movflags", "+faststart", "-an", filepath],
                capture_output=True, timeout=15,
            )
            os.unlink(raw_path)
        except Exception:
            # Fallback: use the raw file if ffmpeg isn't available
            os.rename(raw_path, filepath)

        ai_state.stash_clip(filename)

        # Get scene context (per-camera state key) for the AI to describe
        context = {"camera": target_cam, "camera_name": _camera_name(target_cam)}
        try:
            state_key = _camera_key(_STATE_TMPL, target_cam)
            state = ctx.r.hgetall(state_key)
            if state:
                context["people_in_frame"] = int(state.get("num_people", 0))
                persons_raw = state.get("people", "[]")
                try:
                    persons = json.loads(persons_raw)
                    if persons:
                        context["persons"] = persons
                except (json.JSONDecodeError, TypeError):
                    pass
        except Exception:
            pass

        now = datetime.now(TZ_LOCAL)
        context["timestamp"] = now.strftime("%I:%M %p, %B %d %Y")
        context["duration_seconds"] = 5
        context["size_kb"] = round(len(mp4_bytes) / 1024, 1)

        return json.dumps({
            "clip_captured": True,
            "context": context,
            "instruction": "A 5-second video clip has been recorded and will be shown to the user automatically. Describe what you know from the scene context. Do NOT try to embed the video data.",
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_find_dvr_segment(args: dict) -> str:
    """Find the DVR (.ts) segment that covers a given camera + date + time.
    Returns the segment metadata and a deep-link URL that opens the DVR tab
    on ai.html pre-loaded to that segment.

    This tool does NOT extract or send video bytes — that's what the DVR tab
    is for. The AI's job is to tell the user *which* segment to watch and
    hand them a clickable link.
    """
    import re
    from pathlib import Path

    args = args or {}
    camera_arg = args.get("camera", "")
    date_str = (args.get("date") or "").strip() or "today"
    time_str = (args.get("time") or "").strip()

    # Resolve to a single camera (clip-style — one camera at a time)
    cam_ids = _resolve_camera(camera_arg)
    if not cam_ids:
        return json.dumps({
            "error": f"Unknown camera '{camera_arg}'. Use a registered camera id.",
            "available": [c["id"] for c in _get_camera_list()],
        })
    cam = cam_ids[0]

    # Resolve the date
    now = datetime.now(TZ_LOCAL)
    if date_str == "today":
        target_date = now.date()
    elif date_str == "yesterday":
        target_date = (now - timedelta(days=1)).date()
    else:
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return json.dumps({"error": f"Invalid date '{date_str}'. Use 'today', 'yesterday', or YYYY-MM-DD."})

    # Parse time — accept "13:00", "1:00 PM", "13", or empty (= whole day)
    target_minutes = None
    if time_str:
        t = time_str.strip().upper().replace(" ", "")
        m = re.match(r"^(\d{1,2})(?::(\d{2}))?(AM|PM)?$", t)
        if not m:
            return json.dumps({"error": f"Invalid time '{time_str}'. Use HH:MM (24h) or H:MMam/pm."})
        hh = int(m.group(1))
        mm = int(m.group(2) or 0)
        ampm = m.group(3)
        if ampm == "AM" and hh == 12:
            hh = 0
        elif ampm == "PM" and hh < 12:
            hh += 12
        if not (0 <= hh < 24 and 0 <= mm < 60):
            return json.dumps({"error": f"Time out of range: {time_str}"})
        target_minutes = hh * 60 + mm

    # Scan the per-camera/per-date recordings directory
    day_dir = Path(f"/data/recordings/{cam}/{target_date}")
    if not day_dir.is_dir():
        return json.dumps({
            "error": f"No recordings found for {cam} on {target_date}",
            "hint": "Check /api/recordings/dates?camera=<id> for available dates.",
        })

    # Parse filenames: "HH-MM.ts" where HH-MM is the segment START time
    seg_re = re.compile(r"^(\d{2})-(\d{2})\.ts$")
    segments = []
    for f in sorted(day_dir.iterdir()):
        m = seg_re.match(f.name)
        if not m or not f.is_file():
            continue
        h, mn = int(m.group(1)), int(m.group(2))
        segments.append({
            "filename": f.name,
            "start_minutes": h * 60 + mn,
            "start_label": f"{(h % 12 or 12)}:{mn:02d} {'PM' if h >= 12 else 'AM'}",
            "size_mb": round(f.stat().st_size / (1024 * 1024), 1),
        })

    if not segments:
        return json.dumps({"error": f"No .ts segments in {day_dir}"})

    deep_link_base = f"/ai.html?tab=recordings&camera={cam}&date={target_date}"

    # If no specific time requested, return the segment list
    if target_minutes is None:
        return json.dumps({
            "camera": cam,
            "date": str(target_date),
            "segments_available": len(segments),
            "segments": [
                {
                    "filename": s["filename"],
                    "starts_at": s["start_label"],
                    "size_mb": s["size_mb"],
                    "deep_link": f"{deep_link_base}&segment={s['filename']}",
                }
                for s in segments
            ],
            "note": "Pass `time` (e.g. '13:00') to pick a single best-match segment.",
        })

    # Find the segment whose start time is the largest start <= target_minutes
    best = None
    for s in segments:
        if s["start_minutes"] <= target_minutes:
            if best is None or s["start_minutes"] > best["start_minutes"]:
                best = s
    if best is None:
        # Target time is before the first recording — return the earliest one
        best = segments[0]
        note = (
            f"Requested time {time_str} is before the first recording of the day "
            f"({best['start_label']}). Returning the earliest segment."
        )
    else:
        note = (
            f"Segment starts at {best['start_label']} and runs ~1 hour. "
            f"Click the deep_link to open it in the DVR tab."
        )

    return json.dumps({
        "camera": cam,
        "camera_name": _camera_name(cam),
        "date": str(target_date),
        "requested_time": time_str,
        "segment": best["filename"],
        "segment_starts_at": best["start_label"],
        "size_mb": best["size_mb"],
        "deep_link": f"{deep_link_base}&segment={best['filename']}",
        "note": note,
    })


async def _tool_analyze_image(args: dict) -> str:
    """Analyze the current camera frame with MiniCPM-V vision model."""
    from routes.notifications import get_latest_frame, describe_scene
    import base64

    try:
        frame = get_latest_frame()
        if not frame:
            return json.dumps({"error": "No frame available — camera may be offline"})

        # Also stash the snapshot so it shows in the chat
        b64 = base64.b64encode(frame).decode("utf-8")
        ai_state.stash_snapshot(b64)

        prompt = args.get("prompt", "") or (
            "Describe this security camera image in detail. "
            "Include: time of day (lighting), weather conditions if visible, "
            "any people (count, appearance, actions), vehicles, "
            "and anything notable or unusual."
        )

        description = await describe_scene(frame, prompt=prompt, timeout=30.0)

        if not description:
            return json.dumps({
                "snapshot_captured": True,
                "vision_analysis": "(Vision model timed out or returned empty)",
                "instruction": "The snapshot is shown to the user. The vision model could not produce a description. Describe what you can from any available context.",
            })

        return json.dumps({
            "snapshot_captured": True,
            "vision_analysis": description,
            "instruction": "The snapshot is shown to the user. The 'vision_analysis' field contains a detailed description from the MiniCPM-V vision model of what the camera currently sees. Use this to answer the user's question. You may summarize or enhance the description.",
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_query_notification_history(args: dict) -> str:
    """Get recent notification records (events that triggered Telegram alerts). Multi-camera aware."""
    from contracts.streams import EVENT_STREAM as _EVT_TMPL

    count = min(int(args.get("count", 20)), 50)
    camera_arg = args.get("camera", "")
    cam_ids = _resolve_camera(camera_arg)
    if not cam_ids:
        return json.dumps({
            "error": f"Unknown camera '{camera_arg}'. Use 'all' or a registered camera id.",
            "available": [c["id"] for c in _get_camera_list()],
        })

    try:
        # Sweep enough events across cameras to find alert_triggered ones.
        # Over-fetch generously since most events don't trigger alerts.
        SWEEP_PER_CAM = max(count * 10, 200)
        all_alerts = []
        scanned_total = 0
        for cid in cam_ids:
            evt_key = _camera_key(_EVT_TMPL, cid)
            events_raw = ctx.r.xrevrange(evt_key, count=SWEEP_PER_CAM)
            scanned_total += len(events_raw)
            for msg_id, data in events_raw:
                if data.get("alert_triggered") in ("true", "1"):
                    all_alerts.append({
                        "event_id": msg_id,
                        "camera_id": cid,
                        "camera_name": _camera_name(cid),
                        "type": data.get("event_type", "unknown"),
                        "person_id": data.get("person_id", ""),
                        "identity": data.get("identity_name", ""),
                        "zone": data.get("zone", ""),
                        "timestamp": data.get("timestamp", ""),
                        "alert_level": data.get("alert_level", ""),
                    })

        # Sort newest-first by event_id (which encodes ms timestamp)
        all_alerts.sort(key=lambda a: a["event_id"], reverse=True)
        capped = all_alerts[:count]

        # Aggregate breakdowns
        by_type = {}
        by_identity = {}
        for a in all_alerts:
            t = a["type"]
            by_type[t] = by_type.get(t, 0) + 1
            if a["identity"]:
                by_identity[a["identity"]] = by_identity.get(a["identity"], 0) + 1

        return json.dumps({
            "notifications": capped,
            "alerts_shown": len(capped),
            "alerts_found_in_sweep": len(all_alerts),
            "events_scanned_per_camera": SWEEP_PER_CAM,
            "events_scanned_total": scanned_total,
            "by_type": by_type,
            "by_identity": by_identity,
            "cameras_queried": cam_ids,
            "truncated": len(all_alerts) > count,
            "note": (
                f"Showing newest {len(capped)} of {len(all_alerts)} alerts found in the most recent "
                f"{SWEEP_PER_CAM} events per camera. For older alerts, the Redis stream may have "
                f"trimmed them — use query_events_by_date for a specific date."
            ),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


async def _tool_query_faces() -> str:
    """Query enrolled faces via the face recognizer API."""
    import httpx
    from collections import Counter
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{ctx.FACE_API_URL}/api/faces", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            face_list = data.get("faces", data) if isinstance(data, dict) else data
            if not isinstance(face_list, list):
                face_list = []
            # Group by name — each photo angle is a separate DB row
            name_counts = Counter(
                f.get("name", "unknown") for f in face_list if isinstance(f, dict)
            )
            people = [
                {"name": name, "photos": count}
                for name, count in name_counts.most_common()
            ]
            return json.dumps({
                "enrolled_people": len(people),
                "faces": people,
            })
        return json.dumps({"error": f"Face API returned {resp.status_code}"})
    except Exception as e:
        return json.dumps({"error": str(e)})





# Rate-limiter for the send_telegram tool. A prompt-injected message in
# the chat history could otherwise instruct the LLM to spam the user's
# Telegram. Tracks the last N successful sends in a deque; refuses a new
# call when the oldest send within the window is younger than the limit.
_SEND_TG_WINDOW_SEC = 60.0
_SEND_TG_MAX_PER_WINDOW = 5
_send_tg_history: collections.deque[float] = collections.deque(
    maxlen=_SEND_TG_MAX_PER_WINDOW
)
_send_tg_lock = threading.Lock()


def _send_telegram_rate_check() -> tuple[bool, float]:
    """Returns (allowed, seconds_to_wait). Records timestamp on allow."""
    now = time.time()
    with _send_tg_lock:
        # Drop stale entries
        while _send_tg_history and now - _send_tg_history[0] > _SEND_TG_WINDOW_SEC:
            _send_tg_history.popleft()
        if len(_send_tg_history) >= _SEND_TG_MAX_PER_WINDOW:
            wait = _SEND_TG_WINDOW_SEC - (now - _send_tg_history[0])
            return False, wait
        _send_tg_history.append(now)
        return True, 0.0


async def _tool_send_telegram(args: dict) -> str:
    """Send a Telegram message, optionally with a live snapshot or video clip."""
    from routes.notifications import (
        send_text, send_photo, send_video, is_configured,
        get_latest_frame, build_clip,
    )

    message = args.get("message", "")
    if not message:
        return json.dumps({"error": "No message provided"})
    # Cap message length so a misbehaving model can't dump multi-KB
    # payloads at Telegram (which would 413 anyway, but cleaner to
    # reject early). 4000 is well under Telegram's 4096 char limit.
    if len(message) > 4000:
        return json.dumps({"error": "Message too long (max 4000 chars)"})
    if not is_configured():
        return json.dumps({"error": "Telegram not configured"})

    # Per-tool rate limit: prevents prompt-injection from spamming.
    allowed, wait = _send_telegram_rate_check()
    if not allowed:
        return json.dumps({
            "error": (
                f"Telegram rate limit hit ({_SEND_TG_MAX_PER_WINDOW} sends per "
                f"{int(_SEND_TG_WINDOW_SEC)}s). Retry in {wait:.0f}s."
            )
        })

    # Resolve the source camera for media (snapshot/clip). For text-only, this is ignored.
    cam_ids = _resolve_camera(args.get("camera", ""))
    source_camera = cam_ids[0] if cam_ids else ctx.CAMERA_ID
    source_camera_name = _camera_name(source_camera)

    try:
        include_clip = args.get("include_clip", False)
        include_snapshot = args.get("include_snapshot", False)

        if include_clip:
            clip = build_clip(duration=5.0, fps=10, camera_id=source_camera)
            if clip:
                msg_id = await send_video(clip, f"🎬 {message}")
                return json.dumps({
                    "status": "sent_with_clip", "message": message, "message_id": msg_id,
                    "source_camera_id": source_camera, "source_camera_name": source_camera_name,
                })
            else:
                await send_text(f"{message}\n\n(Video clip unavailable — camera may be offline)")
                return json.dumps({
                    "status": "sent_text_only", "message": message,
                    "source_camera_id": source_camera, "note": "Clip capture failed",
                })

        if include_snapshot:
            frame = get_latest_frame(camera_id=source_camera)
            if frame:
                msg_id = await send_photo(frame, message)
                return json.dumps({
                    "status": "sent_with_snapshot", "message": message, "message_id": msg_id,
                    "source_camera_id": source_camera, "source_camera_name": source_camera_name,
                })
            else:
                await send_text(f"{message}\n\n(Snapshot unavailable — camera may be offline)")
                return json.dumps({
                    "status": "sent_text_only", "message": message,
                    "source_camera_id": source_camera, "note": "No frame available",
                })

        await send_text(message)
        return json.dumps({"status": "sent", "message": message})
    except Exception as e:
        return json.dumps({"error": str(e)})


# Max pending reminders. Prevents prompt-injection from scheduling
# thousands of pre-dated reminders that all fire at once.
_MAX_PENDING_REMINDERS = 50


def _tool_schedule_reminder(args: dict) -> str:
    """Schedule a future Telegram reminder, optionally with media."""
    if not ai_state._ai_db:
        return json.dumps({"error": "AI DB not initialized"})

    message = args.get("message", "")
    time_desc = args.get("time_description", "")
    media_type = args.get("media_type", "text")
    if media_type not in ("text", "snapshot", "clip"):
        media_type = "text"
    if not message or not time_desc:
        return json.dumps({"error": "message and time_description required"})
    if len(message) > 1000:
        return json.dumps({"error": "Reminder message too long (max 1000 chars)"})

    # Refuse to schedule more than _MAX_PENDING_REMINDERS unsent reminders.
    try:
        pending = ai_state._ai_db.count_pending_reminders()
        if pending >= _MAX_PENDING_REMINDERS:
            return json.dumps({
                "error": (
                    f"Too many pending reminders ({pending}). Delete or wait for some "
                    f"to fire before scheduling more (max {_MAX_PENDING_REMINDERS})."
                )
            })
    except AttributeError:
        # ai_db may not expose count_pending_reminders on older versions —
        # if so, skip the check (graceful degradation).
        pass

    # Parse time — try ISO format first, then common patterns
    trigger_time = _parse_time(time_desc)
    if not trigger_time:
        return json.dumps({"error": f"Could not parse time: {time_desc}"})

    reminder_id = ai_state._ai_db.add_reminder(message, trigger_time, media_type=media_type)
    dt = datetime.fromtimestamp(trigger_time, tz=TZ_LOCAL)
    media_label = {"text": "text only", "snapshot": "with snapshot", "clip": "with 5s video clip"}
    return json.dumps({
        "status": "scheduled",
        "reminder_id": reminder_id,
        "message": message,
        "media_type": media_type,
        "scheduled_for": dt.strftime("%I:%M %p, %b %d"),
        "note": media_label.get(media_type, media_type),
    })


def _parse_time(time_desc: str) -> float | None:
    """Parse a time description into a Unix timestamp."""
    now = datetime.now(TZ_LOCAL)

    # Try ISO format
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(time_desc, fmt).replace(tzinfo=TZ_LOCAL)
            return dt.timestamp()
        except ValueError:
            pass

    # Try time-only formats (assume today or next occurrence)
    for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M"):
        try:
            parsed = datetime.strptime(time_desc.strip(), fmt)
            dt = now.replace(hour=parsed.hour, minute=parsed.minute, second=0)
            if dt <= now:
                dt = dt + timedelta(days=1)  # Next day (safe across month boundaries)
            return dt.timestamp()
        except ValueError:
            pass

    # Try relative: "in X minutes/hours"
    import re
    match = re.match(r"in\s+(\d+)\s+(minute|minutes|min|hour|hours|hr)", time_desc.lower())
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        if "hour" in unit or "hr" in unit:
            amount *= 3600
        else:
            amount *= 60
        return (now.timestamp() + amount)

    return None


def _tool_get_system_status(args: dict = None) -> str:
    """Get system status from Redis. Multi-camera aware.

    Default = aggregate across all enabled cameras (most useful for "how's
    the system?" type queries). Pass camera=<id> to scope to one.
    """
    from contracts.streams import (
        EVENT_STREAM as _EVT_TMPL,
        CONFIG_KEY as _CFG_TMPL,
        STATE_KEY as _STATE_TMPL,
    )

    args = args or {}
    camera_arg = args.get("camera", "")
    # System status defaults to "all" — different from per-camera tools.
    if not camera_arg:
        camera_arg = "all"
    cam_ids = _resolve_camera(camera_arg)
    if not cam_ids:
        return json.dumps({
            "error": f"Unknown camera '{camera_arg}'. Use 'all' or a registered camera id.",
            "available": [c["id"] for c in _get_camera_list()],
        })

    try:
        per_camera = {}
        total_events = 0
        for cid in cam_ids:
            evt_key = _camera_key(_EVT_TMPL, cid)
            cfg_key = _camera_key(_CFG_TMPL, cid)
            state_key = _camera_key(_STATE_TMPL, cid)
            ev_len = ctx.r.xlen(evt_key)
            per_camera[cid] = {
                "name": _camera_name(cid),
                "events_in_stream": ev_len,
                "config": _redact_sensitive(ctx.r.hgetall(cfg_key)),
                "state": ctx.r.hgetall(state_key),
            }
            total_events += ev_len

        # Single-camera response keeps the legacy flat shape so existing prompts work
        if len(cam_ids) == 1:
            entry = per_camera[cam_ids[0]]
            return json.dumps({
                "camera": cam_ids[0],
                "camera_name": entry["name"],
                "events_in_stream": entry["events_in_stream"],
                "config": entry["config"],
                "state": entry["state"],
            })
        return json.dumps({
            "cameras_queried": cam_ids,
            "total_events_across_cameras": total_events,
            "per_camera": per_camera,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_query_activity_heatmap(args: dict) -> str:
    """Day-of-week × hour-of-day activity heatmap. Multi-camera aware (aggregates)."""
    from collections import defaultdict
    from contracts.streams import EVENT_STREAM as _EVT_TMPL

    days_back = min(int(args.get("days_back", 14)), 30)
    # Analytical tool — default to all cameras when omitted
    camera_arg = args.get("camera", "all")
    cam_ids = _resolve_camera(camera_arg)
    if not cam_ids:
        return json.dumps({
            "error": f"Unknown camera '{camera_arg}'. Use 'all' or a registered camera id.",
            "available": [c["id"] for c in _get_camera_list()],
        })

    now = datetime.now(TZ_LOCAL)
    start_date = now - timedelta(days=days_back)
    start_ms = int(start_date.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)

    try:
        # Aggregate across all requested cameras
        events_raw = []
        for cid in cam_ids:
            evt_key = _camera_key(_EVT_TMPL, cid)
            events_raw.extend(
                ctx.r.xrange(evt_key, min=f"{start_ms}-0", max=f"{end_ms}-0")
            )

        # Cross-tabulate: day_name -> {hour -> count}
        DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday",
                     "Friday", "Saturday", "Sunday"]
        heatmap = {d: defaultdict(int) for d in DAY_NAMES}
        hourly_total = defaultdict(int)
        daily_total = defaultdict(int)
        weekday_count = 0
        weekend_count = 0

        for msg_id, data in events_raw:
            ts = data.get("timestamp") or data.get("first_seen", "")
            try:
                if "." in str(ts):
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

        # Peak hour overall
        peak_hour = max(hourly_total.items(), key=lambda x: x[1]) if hourly_total else (0, 0)
        # Busiest day
        peak_day = max(daily_total.items(), key=lambda x: x[1]) if daily_total else ("none", 0)

        # Format heatmap as readable grid — always emit all 24 hours per day
        # so the LLM can distinguish "quiet hour" (0) from "missing hour" (unknown).
        grid = {}
        for day in DAY_NAMES:
            grid[day] = {f"{h:02d}:00": heatmap[day].get(h, 0) for h in range(24)}

        # Weekday vs weekend average (per day)
        num_weekdays = max(min(days_back, 30) * 5 // 7, 1)
        num_weekends = max(min(days_back, 30) * 2 // 7, 1)

        return json.dumps({
            "cameras_queried": cam_ids,
            "days_analyzed": days_back,
            "total_events": len(events_raw),
            "peak_hour": f"{peak_hour[0]:02d}:00 ({peak_hour[1]} events)",
            "busiest_day": f"{peak_day[0]} ({peak_day[1]} events)",
            "weekday_total": weekday_count,
            "weekend_total": weekend_count,
            "weekday_avg_per_day": round(weekday_count / num_weekdays, 1),
            "weekend_avg_per_day": round(weekend_count / num_weekends, 1),
            "heatmap": grid,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})




async def _tool_show_faces(args: dict) -> str:
    """Show enrolled face photos — up to 3 per person, sent as images."""
    import base64
    import httpx
    from collections import defaultdict

    filter_name = args.get("name", "").strip().lower()

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{ctx.FACE_API_URL}/api/faces")
            if resp.status_code != 200:
                return json.dumps({"error": f"Face API returned {resp.status_code}"})
            data = resp.json()

        faces = data.get("faces", [])
        if not faces:
            return json.dumps({"message": "No faces enrolled yet."})

        # Group faces by name, keeping their IDs
        by_name = defaultdict(list)
        for f in faces:
            name = f.get("name", "unnamed")
            fid = f.get("id", "")
            if fid:
                by_name[name].append(fid)

        # Filter to specific person if requested
        if filter_name:
            filtered = {
                n: ids for n, ids in by_name.items()
                if filter_name in n.lower()
            }
            if not filtered:
                return json.dumps({
                    "error": f"No enrolled person matching '{filter_name}'",
                    "available": list(by_name.keys()),
                })
            by_name = filtered

        # Fetch up to 3 photos per person and stash as images
        images = []
        summary = []

        async with httpx.AsyncClient(timeout=5.0) as client:
            for name, face_ids in by_name.items():
                photos_to_fetch = face_ids[:3]  # Cap at 3 per person
                fetched = 0
                for fid in photos_to_fetch:
                    try:
                        photo_resp = await client.get(
                            f"{ctx.FACE_API_URL}/api/faces/{fid}/photo"
                        )
                        if photo_resp.status_code == 200:
                            b64 = base64.b64encode(photo_resp.content).decode("utf-8")
                            angle_label = f"angle {fetched + 1}" if len(photos_to_fetch) > 1 else ""
                            caption = f"{name} {angle_label}".strip()
                            images.append({
                                "url": f"data:image/jpeg;base64,{b64}",
                                "caption": caption,
                            })
                            fetched += 1
                    except Exception:
                        continue
                summary.append(f"{name}: {fetched}/{len(face_ids)} photo(s) shown")

        if images:
            ai_state.stash_images(images)

        return json.dumps({
            "photos_sent": len(images),
            "people": list(by_name.keys()),
            "summary": summary,
            "instruction": "Face photos have been sent to the chat. Describe who is enrolled and how many photos each person has.",
        })
    except Exception as e:
        return json.dumps({"error": str(e)})



