"""
routes/ai_tools/ — split package (was ai_tools.py monolith).

Backward-compat surface:
    from routes.ai_tools import TOOLS, execute_tool
    from routes.ai_tools import KNOWN_EVENT_TYPES, EVENT_CATEGORIES

New code can import specific tools directly:
    from routes.ai_tools.query_events import _tool_query_events, SCHEMA

Each tool module contains both its SCHEMA dict (used by the LLM tool-calling
spec) and its _tool_<name> implementation. The TOOLS list and execute_tool
dispatcher below assemble these into the surfaces the chat endpoint expects.
"""

import json
import logging

from ._shared import (
    KNOWN_EVENT_TYPES,
    KNOWN_EVENT_TYPES_DOC,
    EVENT_CATEGORIES,
    _category_matches,
)

# Each tool module — alphabetical for readability.
from . import analyze_image
from . import browse_vehicles
from . import capture_clip
from . import capture_snapshot
from . import find_dvr_segment
from . import get_live_scene
from . import get_system_status
from . import get_weather
from . import query_activity_heatmap
from . import query_event_patterns
from . import query_events
from . import query_events_by_date
from . import query_faces
from . import query_notification_history
from . import query_unknowns
from . import query_zones
from . import schedule_reminder
from . import send_telegram
from . import show_faces

# JSON-schema spec passed to Ollama. Order matters only for which tool
# the LLM "sees first" — alphabetical here.
TOOLS = [
    analyze_image.SCHEMA,
    browse_vehicles.SCHEMA,
    capture_clip.SCHEMA,
    capture_snapshot.SCHEMA,
    find_dvr_segment.SCHEMA,
    get_live_scene.SCHEMA,
    get_system_status.SCHEMA,
    get_weather.SCHEMA,
    query_activity_heatmap.SCHEMA,
    query_event_patterns.SCHEMA,
    query_events.SCHEMA,
    query_events_by_date.SCHEMA,
    query_faces.SCHEMA,
    query_notification_history.SCHEMA,
    query_unknowns.SCHEMA,
    query_zones.SCHEMA,
    schedule_reminder.SCHEMA,
    send_telegram.SCHEMA,
    show_faces.SCHEMA,
]


# Public surface — listed explicitly so the lint gate doesn't flag the
# `_shared` re-exports above as unused. The docstring at the top of this
# file is the contract; this list mirrors it.
__all__ = [
    "TOOLS",
    "execute_tool",
    "KNOWN_EVENT_TYPES",
    "KNOWN_EVENT_TYPES_DOC",
    "EVENT_CATEGORIES",
    "_category_matches",
]


_logger = logging.getLogger("dashboard.ai")


async def execute_tool(name: str, args: dict) -> str:
    """Dispatch an LLM tool call to the matching module's `_tool_<name>` impl.

    Returns a JSON string (every tool wraps its result with json.dumps).
    Async vs sync dispatch matches the underlying tool's signature.
    Argless tools (get_live_scene, get_weather, query_faces, query_unknowns)
    are called without args even when the LLM passes some.
    """
    try:
        # Async, takes args
        if name == "analyze_image":
            return await analyze_image._tool_analyze_image(args)
        if name == "capture_snapshot":
            return await capture_snapshot._tool_capture_snapshot(args)
        if name == "send_telegram":
            return await send_telegram._tool_send_telegram(args)
        if name == "show_faces":
            return await show_faces._tool_show_faces(args)

        # Async, no args
        if name == "get_weather":
            return await get_weather._tool_get_weather()
        if name == "query_faces":
            return await query_faces._tool_query_faces()
        if name == "query_unknowns":
            return await query_unknowns._tool_query_unknowns()

        # Sync, takes args
        if name == "browse_vehicles":
            return browse_vehicles._tool_browse_vehicles(args)
        if name == "capture_clip":
            return capture_clip._tool_capture_clip(args)
        if name == "find_dvr_segment":
            return find_dvr_segment._tool_find_dvr_segment(args)
        if name == "get_system_status":
            return get_system_status._tool_get_system_status(args)
        if name == "query_activity_heatmap":
            return query_activity_heatmap._tool_query_activity_heatmap(args)
        if name == "query_event_patterns":
            return query_event_patterns._tool_query_event_patterns(args)
        if name == "query_events":
            return query_events._tool_query_events(args)
        if name == "query_events_by_date":
            return query_events_by_date._tool_query_events_by_date(args)
        if name == "query_notification_history":
            return query_notification_history._tool_query_notification_history(args)
        if name == "query_zones":
            return query_zones._tool_query_zones(args)
        if name == "schedule_reminder":
            return schedule_reminder._tool_schedule_reminder(args)

        # Sync, no args
        if name == "get_live_scene":
            return get_live_scene._tool_get_live_scene()

        return json.dumps({"error": f"Unknown tool: {name}"})
    except Exception as e:
        _logger.warning(f"Tool {name} error: {e}")
        return json.dumps({"error": str(e)})
