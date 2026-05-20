"""
services/dashboard/event_renderer.py — display formatting for events.

PURPOSE:
    Single source of truth for how an event is presented to a human.
    Used by:
      - routes/events.py     — injects `render` field into /api/events response
                                so the frontend events.js doesn't repeat
                                switch-on-event_type logic
      - routes/bot_commands.py — _cmd_events Telegram formatter

WHY THIS MODULE:
    Before this lived in TWO places (events.js and bot_commands.py _cmd_events)
    with subtle drift. Adding a new event type meant editing both. Now there's
    one place: render_event() returns a structured dict; consumers build their
    UI/message text from that.

OUTPUT SHAPE:
    {
        "icon": "🚨",                     # one emoji
        "title": "Person Appeared — Alex", # primary line
        "subtitle": "2:30 PM · 5s · ...",  # already-joined secondary line
        "css_classes": "appeared alert",   # extra space-separated classes for
                                            # the frontend .event-item element
        "alert": true,                     # should this row stand out red
        "photo": {                         # how to fetch a thumbnail (or None)
            "kind": "face" | "event_snapshot" | "vehicle" | null,
            "identity_name": "Alex"  | null,
            "event_id": "1746...-0"  | null,
            "camera_id": "cam1"| null,
            "snapshot_key": "vehicle:..." | null,
            "caption": "Alex"        | null,
        },
    }
"""

import os
from datetime import datetime
from contracts.tz import TZ_LOCAL  # validated single source of truth

_VEHICLE_ICONS = {"car": "🚗", "truck": "🚛", "motorcycle": "🏍️", "bus": "🚌"}


def _format_time(timestamp_raw) -> str:
    """Best-effort: convert a unix-seconds float to local 12-hour clock."""
    try:
        ts = float(timestamp_raw)
        return datetime.fromtimestamp(ts, tz=TZ_LOCAL).strftime("%I:%M %p")
    except (ValueError, TypeError, OSError):
        return str(timestamp_raw) if timestamp_raw else ""


def _bool(val) -> bool:
    """Redis stream values come through as strings; canonicalize truthiness."""
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ("true", "1", "yes")
    return bool(val)


def render_event(evt: dict) -> dict:
    """Compute display-ready fields for one event. Pure function — no Redis,
    no I/O, no formatting choices that depend on Telegram-vs-frontend. The
    consumer turns these fields into HTML or message text.
    """
    et = evt.get("event_type", "")
    identity = evt.get("identity_name") or ""
    person_id = evt.get("person_id") or ""
    display_name = identity or person_id or "unknown"
    zone = evt.get("zone") or ""
    alert = _bool(evt.get("alert_triggered"))
    time_str = _format_time(evt.get("timestamp"))
    duration = evt.get("duration") or "0"

    # Defaults
    icon = "📌"
    title = et.replace("_", " ").title()
    parts: list[str] = []
    css_classes: list[str] = []
    photo: dict | None = None

    if et == "person_appeared" or et == "person_left":
        is_appeared = et == "person_appeared"
        css_classes.append("appeared" if is_appeared else "left")
        icon = "🚨" if alert else ("🟢" if is_appeared else "🟡")
        title = f"{'Person Appeared' if is_appeared else 'Person Left'} — {display_name}"
        if time_str: parts.append(time_str)
        if duration and duration != "0": parts.append(f"{duration}s")
        if evt.get("direction"): parts.append(evt["direction"])
        action = evt.get("action")
        if action and action != "unknown": parts.append(action)
        if zone: parts.append(f"📍{zone}")
        if alert: parts.append("🚨 Alert")
        # Photo: prefer the known-face thumbnail (consumer resolves identity →
        # face_id), else fall back to the event snapshot saved on disk.
        if identity:
            photo = {"kind": "face", "identity_name": identity,
                     "event_id": None, "camera_id": evt.get("camera_id") or None,
                     "snapshot_key": None, "caption": display_name}
        else:
            photo = {"kind": "event_snapshot", "identity_name": None,
                     "event_id": evt.get("id"),
                     "camera_id": evt.get("camera_id") or None,
                     "snapshot_key": None,
                     "caption": display_name or "Camera snapshot"}

    elif et == "person_identified":
        css_classes.append("appeared")
        icon = "👤"
        title = f"Person Identified — {display_name}"
        if time_str: parts.append(time_str)
        if person_id and identity: parts.append(f"{person_id} recognized as {identity}")
        action = evt.get("action")
        if action and action != "unknown": parts.append(action)
        if zone: parts.append(f"📍{zone}")
        photo = {"kind": "face", "identity_name": identity,
                 "event_id": None, "camera_id": evt.get("camera_id") or None,
                 "snapshot_key": None, "caption": display_name}

    elif et == "face_reconciled":
        css_classes.append("appeared")
        icon = "🔗"
        title = f"Face Matched — {display_name}"
        if time_str: parts.append(time_str)
        action = evt.get("action")
        parts.append(action or "Reconciled unknown")
        photo = {"kind": "face", "identity_name": identity,
                 "event_id": None, "camera_id": evt.get("camera_id") or None,
                 "snapshot_key": None, "caption": display_name}

    elif et == "face_enrolled":
        css_classes.append("appeared")
        icon = "✅"
        title = f"Face Enrolled — {display_name}"
        if time_str: parts.append(time_str)
        parts.append(evt.get("action") or "New enrollment")
        photo = {"kind": "face", "identity_name": identity or person_id,
                 "event_id": None, "camera_id": evt.get("camera_id") or None,
                 "snapshot_key": None, "caption": display_name}

    elif et == "action_changed":
        css_classes.append("appeared")
        icon = "🔄"
        title = f"Action Changed — {display_name}"
        if time_str: parts.append(time_str)
        prev = evt.get("prev_action") or "?"
        action = evt.get("action") or "?"
        parts.append(f"{prev} → {action}")
        if zone: parts.append(f"📍{zone}")

    elif et in ("vehicle_detected", "vehicle_idle"):
        is_idle = et == "vehicle_idle"
        if is_idle:
            css_classes.append("alert")
            icon = "🚨"
        else:
            css_classes.append("appeared")
            icon = _VEHICLE_ICONS.get(evt.get("vehicle_class") or "vehicle", "🚗")
        vclass = evt.get("vehicle_class") or "vehicle"
        prefix = "Vehicle Idling" if is_idle else "Vehicle Detected"
        title = f"{prefix} — {vclass.capitalize()}"
        if time_str: parts.append(time_str)
        if is_idle and duration and duration != "0":
            parts.append(f"⏱️ {duration}s")
        try:
            conf = float(evt.get("vehicle_confidence") or 0)
            if conf:
                parts.append(f"{conf * 100:.0f}% confidence")
        except (ValueError, TypeError):
            pass
        if zone: parts.append(f"📍{zone}")
        if is_idle or alert: parts.append("🚨 Alert")
        if evt.get("snapshot_key"):
            photo = {"kind": "vehicle", "identity_name": None, "event_id": None,
                     "camera_id": evt.get("camera_id") or None,
                     "snapshot_key": evt["snapshot_key"],
                     "caption": f"Vehicle — {vclass}"}

    elif et == "stream_stale":
        css_classes.append("alert")
        icon = "📡"
        title = "Camera Stream Stale"
        if time_str: parts.append(time_str)
        reason = evt.get("reason") or "no frames"
        parts.append(reason)
        parts.append("🚨 Camera may be offline")

    elif et == "stream_recovered":
        css_classes.append("appeared")
        icon = "✅"
        title = "Camera Stream Recovered"
        if time_str: parts.append(time_str)
        parts.append(evt.get("reason") or "frames flowing again")

    elif et == "recorder_error":
        css_classes.append("alert")
        icon = "💾"
        title = "DVR Recorder Failing"
        if time_str: parts.append(time_str)
        parts.append(evt.get("reason") or "ffmpeg keeps crashing")
        parts.append("🚨 Recordings may be incomplete")

    elif et == "recorder_recovered":
        css_classes.append("appeared")
        icon = "✅"
        title = "DVR Recorder Recovered"
        if time_str: parts.append(time_str)
        parts.append(evt.get("reason") or "recording stable")

    elif et == "unauthorized_access":
        css_classes.append("alert")
        icon = "🔒"
        tg_user = (f"@{evt['telegram_username']}"
                   if evt.get("telegram_username")
                   else f"ID:{evt.get('telegram_user_id') or '?'}")
        attempted = evt.get("action") or "unknown"
        title = f"Unauthorized Access — {display_name or tg_user}"
        if time_str: parts.append(time_str)
        parts.append(f"{tg_user} tried {attempted}")
        parts.append("🚨 Blocked")

    else:
        # Unknown event type — pass-through with whatever's set
        if time_str: parts.append(time_str)
        if zone: parts.append(f"📍{zone}")

    if alert:
        css_classes.append("alert")

    return {
        "icon": icon,
        "title": title,
        "subtitle": " · ".join(parts),
        "css_classes": " ".join(dict.fromkeys(css_classes)),  # dedupe, preserve order
        "alert": alert or et in ("unauthorized_access", "vehicle_idle", "stream_stale", "recorder_error"),
        "photo": photo,
    }
