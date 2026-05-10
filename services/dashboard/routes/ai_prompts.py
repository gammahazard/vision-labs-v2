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
from datetime import datetime
from zoneinfo import ZoneInfo

import routes as ctx
import routes.ai_state as ai_state

logger = logging.getLogger("dashboard.ai")
TZ_LOCAL = ZoneInfo(os.getenv("LOCATION_TIMEZONE", "America/Toronto"))


# ---------------------------------------------------------------------------
# Build system prompt
# ---------------------------------------------------------------------------
async def build_system_context() -> str:
    """Gather a live system snapshot to inject into the system prompt."""
    parts = []
    # Enrolled faces
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{ctx.FACE_API_URL}/api/faces", timeout=3)
        if resp.status_code == 200:
            data = resp.json()
            # Face-recognizer returns {"faces": [...], "count": N}
            face_list = data.get("faces", data) if isinstance(data, dict) else data
            if not isinstance(face_list, list):
                face_list = []
            names = [f.get("name", "unknown") for f in face_list if isinstance(f, dict)]
            parts.append(f"Enrolled faces ({len(names)}): {', '.join(names) if names else 'none'}")
    except Exception:
        pass
    # Zones
    try:
        zone_data = ctx.r.hgetall(ctx.ZONE_KEY)
        count = len(zone_data) if zone_data else 0
        parts.append(f"Active zones: {count}")
    except Exception:
        pass
    # Event stream size
    try:
        ev_len = ctx.r.xlen(ctx.EVENT_STREAM)
        parts.append(f"Events in stream: {ev_len}")
    except Exception:
        pass
    return "\n".join(parts)


def build_system_prompt(config: dict, system_context: str = "") -> str:
    """Build the system prompt with personality and context."""
    ai_name = config.get("ai_name", "Atlas")
    user_name = config.get("user_name", "")
    user_ref = user_name if user_name else "the user"
    now = datetime.now(TZ_LOCAL)

    name_line = f"\nThe user's name is {user_name}. Address them by name occasionally." if user_name else ""
    context_block = f"\n\nCURRENT SYSTEM SNAPSHOT:\n{system_context}" if system_context else ""

    return f"""You are {ai_name}, a helpful and friendly AI assistant for a home security system called Vision Labs. You run locally on the user's own hardware — no data ever leaves this machine.{name_line}

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

PERSONALITY:
- Conversational and warm, but concise
- Security-aware — flag anything unusual if asked
- When {user_ref} asks about events, use the query_events tool to get real data
- When asked to send a message or set a reminder, use the appropriate tool
- For general questions unrelated to security, answer normally without tools
- Don't use tools unless the question actually requires data lookup

IMPORTANT: Do NOT wrap your response in <think> tags or show your reasoning process. Respond directly and naturally."""
