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

import os
import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import routes as ctx
import routes.ai_state as ai_state

logger = logging.getLogger("dashboard.ai")
TZ_LOCAL = ZoneInfo(os.getenv("LOCATION_TIMEZONE", "America/Toronto"))


# ---------------------------------------------------------------------------
# Tool definitions for the LLM
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_events",
            "description": "Search recent security events (person detected, person identified, vehicle idle). Returns the most recent events.",
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Number of recent events to return (max 50)",
                    },
                    "event_type": {
                        "type": "string",
                        "description": "Filter by event type: person_appeared, person_identified, person_left, vehicle_idle. Leave empty for all.",
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
            "description": "Send a message to the user via Telegram right now. Can include a live camera snapshot or a 5-second video clip.",
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
            "description": "Get current system status: stream sizes, config settings, notification preferences.",
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
            "description": "Query events filtered by date. Use this to answer questions like 'how many events today' or 'what happened yesterday'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "Date to query in YYYY-MM-DD format. Use 'today' or 'yesterday' as shortcuts.",
                    },
                    "event_type": {
                        "type": "string",
                        "description": "Optional: filter by event type (person_appeared, person_identified, person_left, vehicle_idle)",
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
            "description": "List all security zones defined on the camera, including their names, alert levels, and point coordinates.",
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
            "name": "browse_vehicles",
            "description": "Browse vehicle detection snapshots for a given day. Shows snapshot images inline in the chat. Use when the user asks to see vehicle photos or detections.",
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
            "description": "Analyze event patterns and trends. Groups events by hour of day, by type, or calculates daily averages. Use for questions like 'what's the busiest time of day' or 'how many people per day this week'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "analysis_type": {
                        "type": "string",
                        "enum": ["hourly", "daily", "type_breakdown"],
                        "description": "Type of analysis: 'hourly' (by hour of day), 'daily' (by day), 'type_breakdown' (events by type)",
                    },
                    "days_back": {
                        "type": "integer",
                        "description": "How many days of history to analyze (default 7, max 30)",
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
            "description": "Capture the current camera frame and show it in the chat. Returns context data (weather, scene) for you to describe.",
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
            "name": "capture_clip",
            "description": "Record a 5-second video clip from the live camera and show it in the chat. Use when the user asks to see a clip, video, or recording of what's happening now.",
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
            "name": "query_notification_history",
            "description": "Get recent Telegram notifications that were sent by the system. Shows what alerts the user has received.",
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Number of recent notifications to return (default 20, max 50)",
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
            "description": "Get a day-of-week × hour-of-day activity heatmap. Shows which days and hours are busiest, weekend vs weekday comparison, and peak activity windows.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days_back": {
                        "type": "integer",
                        "description": "How many days of history to analyze (default 14, max 30)",
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
            return _tool_get_system_status()
        elif name == "get_live_scene":
            return _tool_get_live_scene()
        elif name == "query_unknowns":
            return await _tool_query_unknowns()
        elif name == "query_events_by_date":
            return _tool_query_events_by_date(args)
        elif name == "query_zones":
            return _tool_query_zones()
        elif name == "browse_vehicles":
            return _tool_browse_vehicles(args)
        elif name == "get_weather":
            return await _tool_get_weather()
        elif name == "query_event_patterns":
            return _tool_query_event_patterns(args)
        elif name == "capture_snapshot":
            return await _tool_capture_snapshot()
        elif name == "capture_clip":
            return _tool_capture_clip()
        elif name == "query_notification_history":
            return _tool_query_notification_history(args)
        elif name == "query_activity_heatmap":
            return _tool_query_activity_heatmap(args)
        elif name == "show_faces":
            return await _tool_show_faces(args)
        elif name == "analyze_image":
            return await _tool_analyze_image(args)
        else:
            return json.dumps({"error": f"Unknown tool: {name}"})
    except Exception as e:
        logger.warning(f"Tool {name} error: {e}")
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------
def _tool_query_events(args: dict) -> str:
    """Query recent events from Redis."""
    count = min(int(args.get("count", 20)), 50)
    event_type = args.get("event_type", "")

    try:
        events_raw = ctx.r.xrevrange(ctx.EVENT_STREAM, count=count)
        events = []
        for msg_id, data in events_raw:
            evt = {k: v for k, v in data.items()}
            evt["event_id"] = msg_id
            if event_type and evt.get("event_type") != event_type:
                continue
            events.append(evt)
        return json.dumps({"events": events, "total": len(events)})
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_get_live_scene() -> str:
    """Get the current live scene from the tracker state."""
    try:
        state = ctx.r.hgetall(ctx.STATE_KEY)
        if not state:
            return json.dumps({"scene": "No data — tracker may not be running or no activity detected."})

        num_people = int(state.get("num_people", "0"))
        persons_raw = state.get("people", "[]")
        try:
            persons = json.loads(persons_raw)
        except (json.JSONDecodeError, TypeError):
            persons = []

        scene_data = {
            "people_in_frame": num_people,
            "camera": ctx.CAMERA_ID,
        }

        # Only include person/identity details when someone is actually in frame.
        # The identity state in Redis is NOT cleared when people leave, so it
        # would show stale "Unknown" entries and confuse the AI.
        if num_people > 0:
            scene_data["persons"] = persons

            identity_state = ctx.r.hgetall(ctx.IDENTITY_KEY)
            identities = []
            if identity_state:
                try:
                    identities = json.loads(identity_state.get("identities", "[]"))
                except (json.JSONDecodeError, TypeError):
                    identities = []
            if identities:
                scene_data["identified_faces"] = identities
        else:
            scene_data["summary"] = "No people currently in frame. The scene is clear."

        return json.dumps(scene_data)
    except Exception as e:
        return json.dumps({"error": str(e)})


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

        return json.dumps({
            "enrolled_count": len(enrolled_names),
            "enrolled_names": enrolled_names,
            "unknown_count": len(unknowns),
            "unknowns": [{"id": f.get("id", "?"), "first_seen": f.get("first_seen", "?")} for f in unknowns[:20]],
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_query_events_by_date(args: dict) -> str:
    """Query events filtered by date."""
    import time as _time

    date_str = args.get("date", "today")
    event_type = args.get("event_type", "")

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

    try:
        events_raw = ctx.r.xrange(ctx.EVENT_STREAM, min=f"{start_ms}-0", max=f"{end_ms}-0")
        events = []
        for msg_id, data in events_raw:
            evt = {k: v for k, v in data.items()}
            evt["event_id"] = msg_id
            if event_type and evt.get("event_type") != event_type:
                continue
            events.append(evt)

        # Summarize by type
        type_counts = {}
        for evt in events:
            t = evt.get("event_type", "unknown")
            type_counts[t] = type_counts.get(t, 0) + 1

        return json.dumps({
            "date": str(target_date),
            "total_events": len(events),
            "by_type": type_counts,
            "latest_events": events[-10:] if len(events) > 10 else events,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_query_zones() -> str:
    """List all defined security zones."""
    try:
        zone_data = ctx.r.hgetall(ctx.ZONE_KEY)
        if not zone_data:
            return json.dumps({"zones": [], "count": 0, "message": "No zones defined yet."})

        zones = []
        for zone_id, zone_json in zone_data.items():
            try:
                zone = json.loads(zone_json)
                zone["id"] = zone_id
                zones.append(zone)
            except (json.JSONDecodeError, TypeError):
                zones.append({"id": zone_id, "raw": zone_json})

        return json.dumps({"zones": zones, "count": len(zones)})
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_browse_vehicles(args: dict) -> str:
    """List vehicle detection snapshots for a given day and stash images for display."""
    import glob

    date_str = args.get("date", "today")
    count_requested = min(int(args.get("count", 5)), 10)  # Max 10 images
    now = datetime.now(TZ_LOCAL)
    if date_str == "today":
        target_date = now.strftime("%Y-%m-%d")
    elif date_str == "yesterday":
        target_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        target_date = date_str

    snapshot_dir = ctx.VEHICLE_SNAPSHOT_DIR
    day_dir = os.path.join(snapshot_dir, target_date) if snapshot_dir else f"/data/vehicle_snapshots/{target_date}"

    try:
        if not os.path.isdir(day_dir):
            return json.dumps({"date": target_date, "count": 0, "snapshots": [], "message": f"No vehicle snapshots for {target_date}"})

        files = sorted(glob.glob(os.path.join(day_dir, "*.jpg")))
        snapshots = []
        for f in files:
            basename = os.path.basename(f)
            # Parse filename: HH-MM-SS_classname.jpg
            base_name = basename.rsplit(".", 1)[0]
            parts = base_name.split("_", 1)
            time_str = parts[0].replace("-", ":") if parts else ""
            vehicle_class = parts[1] if len(parts) > 1 else "vehicle"
            snapshots.append({
                "filename": basename,
                "time": time_str,
                "vehicle_class": vehicle_class,
                "size_kb": round(os.path.getsize(f) / 1024, 1),
                "url": f"/api/browse/snapshot/{target_date}/{basename}",
            })

        # Stash the last N images for inline display in the chat
        display_snapshots = snapshots[-count_requested:]
        if display_snapshots:
            ai_state.stash_images([
                {"url": s["url"], "caption": f"{s['time']} — {s['vehicle_class']}"}
                for s in display_snapshots
            ])

        return json.dumps({
            "date": target_date,
            "count": len(snapshots),
            "snapshots": snapshots[-20:],  # Last 20 metadata
            "images_will_be_shown": len(display_snapshots),
            "note": f"Showing last {min(20, len(snapshots))} of {len(snapshots)}" if len(snapshots) > 20 else None,
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
    """Analyze event patterns for trends."""
    from collections import defaultdict

    analysis_type = args.get("analysis_type", "hourly")
    days_back = min(int(args.get("days_back", 7)), 30)

    # Calculate time range (timezone-aware for correct local day boundaries)
    now = datetime.now(TZ_LOCAL)
    start_date = now - timedelta(days=days_back)
    start_ms = int(start_date.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)

    try:
        events_raw = ctx.r.xrange(ctx.EVENT_STREAM, min=f"{start_ms}-0", max=f"{end_ms}-0")

        if analysis_type == "hourly":
            hourly = defaultdict(int)
            for msg_id, data in events_raw:
                ts = data.get("timestamp") or data.get("first_seen", "")
                try:
                    if "." in str(ts):
                        dt = datetime.fromtimestamp(float(ts), tz=TZ_LOCAL)
                    else:
                        dt = datetime.fromisoformat(str(ts))
                    hourly[dt.hour] += 1
                except (ValueError, TypeError, OSError):
                    continue

            # Format as readable hours
            result = {}
            for h in range(24):
                label = f"{h:02d}:00"
                result[label] = hourly.get(h, 0)

            busiest = max(hourly.items(), key=lambda x: x[1]) if hourly else (0, 0)
            return json.dumps({
                "analysis": "hourly",
                "days_analyzed": days_back,
                "total_events": len(events_raw),
                "hourly_breakdown": result,
                "busiest_hour": f"{busiest[0]:02d}:00 ({busiest[1]} events)",
            })

        elif analysis_type == "daily":
            daily = defaultdict(int)
            for msg_id, data in events_raw:
                ts = data.get("timestamp") or data.get("first_seen", "")
                try:
                    if "." in str(ts):
                        dt = datetime.fromtimestamp(float(ts), tz=TZ_LOCAL)
                    else:
                        dt = datetime.fromisoformat(str(ts))
                    daily[dt.strftime("%Y-%m-%d")] += 1
                except (ValueError, TypeError, OSError):
                    continue

            avg = sum(daily.values()) / max(len(daily), 1)
            return json.dumps({
                "analysis": "daily",
                "days_analyzed": days_back,
                "total_events": len(events_raw),
                "daily_breakdown": dict(sorted(daily.items())),
                "daily_average": round(avg, 1),
                "busiest_day": max(daily.items(), key=lambda x: x[1])[0] if daily else "none",
            })

        elif analysis_type == "type_breakdown":
            types = defaultdict(int)
            for msg_id, data in events_raw:
                evt_type = data.get("event_type", "unknown")
                types[evt_type] += 1

            return json.dumps({
                "analysis": "type_breakdown",
                "days_analyzed": days_back,
                "total_events": len(events_raw),
                "by_type": dict(sorted(types.items(), key=lambda x: x[1], reverse=True)),
            })

        else:
            return json.dumps({"error": f"Unknown analysis type: {analysis_type}"})
    except Exception as e:
        return json.dumps({"error": str(e)})


async def _tool_capture_snapshot() -> str:
    """Capture camera frame with weather + scene context for AI to describe."""
    import base64
    import httpx
    from routes.notifications import get_latest_frame

    try:
        frame = get_latest_frame()
        if not frame:
            return json.dumps({"error": "No frame available — camera may be offline"})

        b64 = base64.b64encode(frame).decode("utf-8")

        # Stash the base64 for the chat handler — NOT for the LLM
        ai_state.stash_snapshot(b64)

        # Gather contextual data so the AI can describe the scene intelligently
        context = {}

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

        # Current scene state
        try:
            state = ctx.r.hgetall(ctx.STATE_KEY)
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

        # Return ONLY the small metadata to the LLM — no base64!
        return json.dumps({
            "snapshot_captured": True,
            "size_kb": round(len(frame) / 1024, 1),
            "context": context,
            "instruction": "A live camera snapshot has been captured and will be shown to the user automatically. Describe what you know from the context data: weather conditions, who is in frame, time of day. Do NOT try to include or reference the image data — it is handled for you.",
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_capture_clip() -> str:
    """Capture 5-second MP4 clip from the live camera."""
    from routes.notifications import build_clip
    import uuid as _uuid

    try:
        mp4_bytes = build_clip(duration=5.0, fps=10)
        if not mp4_bytes:
            return json.dumps({"error": "Clip capture failed — camera may be offline or not enough frames"})

        # Save raw OpenCV clip to temp file first
        clip_dir = os.path.join("/data/snapshots", "clips")
        os.makedirs(clip_dir, exist_ok=True)
        filename = f"{datetime.now(TZ_LOCAL).strftime('%Y%m%d_%H%M%S')}_{_uuid.uuid4().hex[:6]}.mp4"
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

        # Get scene context for the AI to describe
        context = {}
        try:
            state = ctx.r.hgetall(ctx.STATE_KEY)
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
    """Get recent notification records from the feedback database."""
    count = min(int(args.get("count", 20)), 50)

    try:
        # Check for recent events that triggered notifications
        events_raw = ctx.r.xrevrange(ctx.EVENT_STREAM, count=count * 3)  # Over-fetch to find notified ones
        notified = []
        for msg_id, data in events_raw:
            if data.get("alert_triggered") == "true" or data.get("alert_triggered") == "1":
                notified.append({
                    "event_id": msg_id,
                    "type": data.get("event_type", "unknown"),
                    "person_id": data.get("person_id", ""),
                    "identity": data.get("identity_name", ""),
                    "zone": data.get("zone", ""),
                    "timestamp": data.get("timestamp", ""),
                    "alert_level": data.get("alert_level", ""),
                })
                if len(notified) >= count:
                    break

        return json.dumps({
            "notifications": notified,
            "count": len(notified),
            "note": "These events triggered Telegram notifications",
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





async def _tool_send_telegram(args: dict) -> str:
    """Send a Telegram message, optionally with a live snapshot or video clip."""
    from routes.notifications import (
        send_text, send_photo, send_video, is_configured,
        get_latest_frame, build_clip,
    )

    message = args.get("message", "")
    if not message:
        return json.dumps({"error": "No message provided"})
    if not is_configured():
        return json.dumps({"error": "Telegram not configured"})

    try:
        include_clip = args.get("include_clip", False)
        include_snapshot = args.get("include_snapshot", False)

        if include_clip:
            clip = build_clip(duration=5.0, fps=10)
            if clip:
                msg_id = await send_video(clip, f"🎬 {message}")
                return json.dumps({"status": "sent_with_clip", "message": message, "message_id": msg_id})
            else:
                await send_text(f"{message}\n\n(Video clip unavailable — camera may be offline)")
                return json.dumps({"status": "sent_text_only", "message": message, "note": "Clip capture failed"})

        if include_snapshot:
            frame = get_latest_frame()
            if frame:
                msg_id = await send_photo(frame, message)
                return json.dumps({"status": "sent_with_snapshot", "message": message, "message_id": msg_id})
            else:
                await send_text(f"{message}\n\n(Snapshot unavailable — camera may be offline)")
                return json.dumps({"status": "sent_text_only", "message": message, "note": "No frame available"})

        await send_text(message)
        return json.dumps({"status": "sent", "message": message})
    except Exception as e:
        return json.dumps({"error": str(e)})


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


def _tool_get_system_status() -> str:
    """Get system status from Redis."""
    try:
        stats = {
            "events_in_stream": ctx.r.xlen(ctx.EVENT_STREAM),
            "config": ctx.r.hgetall(ctx.CONFIG_KEY),
            "state": ctx.r.hgetall(ctx.STATE_KEY),
        }
        return json.dumps(stats)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_query_activity_heatmap(args: dict) -> str:
    """Day-of-week × hour-of-day activity heatmap."""
    from collections import defaultdict

    days_back = min(int(args.get("days_back", 14)), 30)
    now = datetime.now(TZ_LOCAL)
    start_date = now - timedelta(days=days_back)
    start_ms = int(start_date.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)

    try:
        events_raw = ctx.r.xrange(
            ctx.EVENT_STREAM, min=f"{start_ms}-0", max=f"{end_ms}-0"
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

        # Format heatmap as readable grid
        grid = {}
        for day in DAY_NAMES:
            row = {}
            for h in range(24):
                count = heatmap[day].get(h, 0)
                if count > 0:
                    row[f"{h:02d}:00"] = count
            if row:
                grid[day] = row

        # Weekday vs weekend average (per day)
        num_weekdays = max(min(days_back, 30) * 5 // 7, 1)
        num_weekends = max(min(days_back, 30) * 2 // 7, 1)

        return json.dumps({
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



