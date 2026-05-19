"""
routes/ai_prompts.py — System prompt builder for the AI assistant.

PURPOSE:
    Builds the system prompt injected into every LLM conversation.
    Includes live system context (enrolled faces, zones, events)
    and the AI's personality/capability instructions.
"""

import os
import json
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import routes as ctx
import routes.ai_state as ai_state

logger = logging.getLogger("dashboard.ai")
TZ_LOCAL = ZoneInfo(os.getenv("LOCATION_TIMEZONE", "America/Toronto"))


# Cache for build_system_context. The snapshot includes a Redis hgetall,
# an HTTP call to the face-recognizer, and two more Redis lookups —
# 50-200 ms total per chat turn. Cached for ~30s: registry/faces/zones
# don't change minute-to-minute so stale snapshots are harmless.
_SYSTEM_CONTEXT_TTL_SEC = 30.0
_system_context_cache: tuple[float, str] | None = None


# ---------------------------------------------------------------------------
# Build system prompt
# ---------------------------------------------------------------------------
async def build_system_context() -> str:
    """Gather a live system snapshot to inject into the system prompt."""
    global _system_context_cache
    now = time.time()
    if _system_context_cache is not None:
        cached_at, cached_value = _system_context_cache
        if now - cached_at < _SYSTEM_CONTEXT_TTL_SEC:
            return cached_value
    parts = []
    # Registered cameras — gives the LLM the IDs + names it should use for the
    # `camera` arg on multi-camera tools (query_events, capture_snapshot, etc.)
    try:
        import json as _json
        raw = ctx.r.hgetall("cameras:registry") or {}
        cams = []
        for cid, val in raw.items():
            try:
                e = _json.loads(val)
                if e.get("enabled", True):
                    cams.append(f"{e.get('id','?')}={e.get('name','?')}")
            except Exception:
                pass
        if cams:
            parts.append("Cameras (id=name): " + " · ".join(sorted(cams)))
            parts.append("→ When the user mentions a specific camera by name, pass camera=<id> to tools. Use 'all' for system-wide queries.")
    except Exception:
        pass
    # Enrolled faces (deduped by name — face-recognizer returns one row per photo,
    # so a person enrolled with 30 photos was previously listed 30x)
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{ctx.FACE_API_URL}/api/faces", timeout=3)
        if resp.status_code == 200:
            data = resp.json()
            face_list = data.get("faces", data) if isinstance(data, dict) else data
            if not isinstance(face_list, list):
                face_list = []
            from collections import Counter
            name_counts = Counter(
                f.get("name", "unknown") for f in face_list if isinstance(f, dict)
            )
            unique_names = sorted(name_counts.keys())
            parts.append(
                f"Enrolled people ({len(unique_names)}, "
                f"{sum(name_counts.values())} total photos): "
                f"{', '.join(unique_names) if unique_names else 'none'}"
            )
    except Exception:
        pass
    # Zones + events aggregated across all registered cameras
    # (ctx.ZONE_KEY and ctx.EVENT_STREAM are templates with {camera_id} —
    #  must be formatted per-camera, not queried as literal keys)
    try:
        from contracts.streams import ZONE_KEY as _Z, EVENT_STREAM as _E, stream_key
        raw = ctx.r.hgetall("cameras:registry") or {}
        cam_ids = []
        import json as _json
        for cid, val in raw.items():
            try:
                e = _json.loads(val)
                if e.get("enabled", True):
                    cam_ids.append(e.get("id", cid))
            except Exception:
                pass
        if not cam_ids:
            cam_ids = [ctx.CAMERA_ID]
        total_zones = 0
        total_events = 0
        for cid in cam_ids:
            try:
                total_zones += len(ctx.r.hgetall(stream_key(_Z, camera_id=cid)) or {})
            except Exception:
                pass
            try:
                total_events += ctx.r.xlen(stream_key(_E, camera_id=cid))
            except Exception:
                pass
        parts.append(f"Active zones (all cameras): {total_zones}")
        parts.append(f"Events in stream (all cameras): {total_events}")
    except Exception:
        pass
    result = "\n".join(parts)
    _system_context_cache = (now, result)
    return result


def build_system_prompt(config: dict, system_context: str = "") -> str:
    """Build the system prompt with personality and context."""
    ai_name = config.get("ai_name", "Atlas")
    user_name = config.get("user_name", "")
    user_ref = user_name if user_name else "the user"
    now = datetime.now(TZ_LOCAL)

    name_line = f"\nThe user's name is {user_name}. Address them by name occasionally." if user_name else ""
    context_block = f"\n\nCURRENT SYSTEM SNAPSHOT:\n{system_context}" if system_context else ""

    return f"""⚠️ ABSOLUTE RULE — read first, follow always ⚠️

Before answering ANY question that involves dates, counts, identities, events, cameras, or "how many / who / when / what happened":
  1. You MUST call a tool to fetch fresh data IN THIS TURN.
  2. NEVER copy numbers, names, or counts from earlier assistant messages in the conversation history. Prior answers may be wrong; only THIS turn's tool result is trusted.
  3. If the user asks the same question again, CALL THE TOOL AGAIN. Do not paraphrase your last answer.
  4. After calling the tool, report ONLY what the tool returned. Use the exact numbers from `by_type`, `by_identity`, `total_events`, and `per_camera.<id>.by_identity`. Do not invent or round.

You are {ai_name}, a helpful and friendly AI assistant for a home security system called Vision Labs. You run locally on the user's own hardware — no data ever leaves this machine.{name_line}

Your primary role is helping {user_ref} monitor and manage their security cameras, but you're also happy to help with general questions, reminders, and conversation.

Current time: {now.strftime("%I:%M %p, %A %B %d, %Y")} ({now.tzname()}){context_block}

CAPABILITIES (use tools when relevant):
- Query recent security events (people detected, identified, vehicles)
- Query events by date (today, yesterday, specific date)
- Analyze event patterns — hourly trends, daily averages, busiest times
- Look up enrolled faces and unknown/auto-captured faces
- View the live scene — who's in frame right now, identified faces
- Capture a live camera snapshot and describe it to the user
- Get current weather conditions (temperature, humidity, wind)
- Browse vehicle detection snapshots by day
- Send Telegram messages immediately (with optional live camera snapshot or 5-second video clip)
- Schedule timed reminders via Telegram (with optional snapshot or video clip captured at scheduled time)
- Check system status, configuration, and zone definitions
- View notification history — what Telegram alerts were sent

SNAPSHOT & CLIP HANDLING:
When capture_snapshot or capture_clip returns, the media is automatically displayed to the user — you do NOT need to embed or reference it.
Just describe what you know from the context data the tool returns: weather conditions, who is in frame, time of day.
Do NOT try to output any image/video data or base64 strings.

CRITICAL TOOL-USE RULES (read carefully):
1. ANY question about counts, identities, who/what/when/how-many for security data MUST trigger a tool call. Do not answer from memory or chat history.
2. The conversation history may contain incorrect facts from earlier turns — ALWAYS prefer the result of a fresh tool call over anything stated in prior messages.
3. When a tool returns aggregations like `by_type`, `by_identity`, `total_events`, `unique_people_identified` — use those EXACT numbers. Never invent a breakdown.
4. If `by_identity` is `{{}}` (empty) — say so, do not invent names. If a name isn't in `by_identity`, that person was not identified.
5. When asked "how many detections" — use `total_events` (sum of every event of every type), NOT `person_identified` count.
6. When asked "who was detected/seen" — list EVERY name from `by_identity`, with its exact count. Do not omit, merge, or invent.

PERSONALITY:
- Conversational and warm, but concise
- Security-aware — flag anything unusual if asked
- When {user_ref} asks about events, ALWAYS call query_events_by_date or query_events to get fresh data
- When asked to send a message or set a reminder, use the appropriate tool
- For general questions unrelated to security (e.g. weather chitchat, math), answer normally without tools

IMPORTANT: Do NOT wrap your response in <think> tags or show your reasoning process. Respond directly and naturally."""
