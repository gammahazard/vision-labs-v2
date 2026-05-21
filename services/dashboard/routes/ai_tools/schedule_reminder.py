"""
routes/ai_tools/schedule_reminder.py — implementation + schema for the `schedule_reminder` tool.

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
import time
import re as _re

_MAX_PENDING_REMINDERS = 50

# Map English number words to ints (covers the values an LLM is likely to
# emit; not exhaustive). Lowercased input.
_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
    "forty-five": 45, "sixty": 60, "ninety": 90,
}

# `in <N> (second|minute|hour|day)s?` — both digit and word forms.
_REL_RE = _re.compile(
    r"^in\s+(?P<n>\d+|[a-z\-]+)\s+(?P<unit>second|minute|hour|day)s?\b",
    _re.IGNORECASE,
)

# `HH:MM` or `H:MM` optionally with AM/PM.
_TOD_RE = _re.compile(
    r"^(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ampm>AM|PM)?$",
    _re.IGNORECASE,
)


def _parse_time(time_desc: str) -> int | None:
    """Parse a human/LLM time description into a unix timestamp.

    Supports:
      - ISO 8601 datetime: '2026-02-21T22:00:00' (with or without offset)
      - Relative offsets: 'in 5 minutes', 'in 2 hours', 'in 1 day',
        'in five minutes' (English number words covered in _WORD_NUMBERS)
      - Time of day:      '10:00 PM', '10 PM', '22:00', '8:30 AM' — assumes
        today; if already past, rolls to tomorrow.

    Returns None when the input can't be parsed by any branch.
    """
    if not time_desc:
        return None
    s = time_desc.strip()
    now = datetime.now(TZ_LOCAL)

    # 1. ISO 8601. Accept with or without timezone.
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ_LOCAL)
        return int(dt.timestamp())
    except ValueError:
        pass

    # 2. Relative offset.
    m = _REL_RE.match(s)
    if m:
        n_str = m.group("n").lower()
        if n_str.isdigit():
            n = int(n_str)
        else:
            n = _WORD_NUMBERS.get(n_str)
        if n is None:
            return None
        unit = m.group("unit").lower()
        delta = {
            "second": timedelta(seconds=n),
            "minute": timedelta(minutes=n),
            "hour":   timedelta(hours=n),
            "day":    timedelta(days=n),
        }[unit]
        return int((now + delta).timestamp())

    # 3. Time of day.
    m = _TOD_RE.match(s)
    if m:
        h = int(m.group("h"))
        minute = int(m.group("m") or 0)
        ampm = (m.group("ampm") or "").upper()
        if ampm == "PM" and h < 12:
            h += 12
        elif ampm == "AM" and h == 12:
            h = 0
        if not (0 <= h <= 23 and 0 <= minute <= 59):
            return None
        target = now.replace(hour=h, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return int(target.timestamp())

    return None


SCHEMA ={'type': 'function', 'function': {'name': 'schedule_reminder', 'description': 'Schedule a reminder to be sent via Telegram at a specific time. Can include a snapshot or video clip captured at the scheduled time.', 'parameters': {'type': 'object', 'properties': {'message': {'type': 'string', 'description': 'The reminder message'}, 'time_description': {'type': 'string', 'description': "When to send, e.g. '10:00 PM', 'in 30 minutes', '2026-02-21T22:00:00'"}, 'media_type': {'type': 'string', 'enum': ['text', 'snapshot', 'clip'], 'description': "Type of media to include: 'text' (default), 'snapshot' (camera photo), or 'clip' (5-second video)."}}, 'required': ['message', 'time_description']}}}


def _tool_schedule_reminder(args: dict) -> str:
    """Schedule a future Telegram reminder, optionally with media."""
    if not ai_state._ai_db:
        return json.dumps({'error': 'AI DB not initialized'})
    message = args.get('message', '')
    time_desc = args.get('time_description', '')
    media_type = args.get('media_type', 'text')
    if media_type not in ('text', 'snapshot', 'clip'):
        media_type = 'text'
    if not message or not time_desc:
        return json.dumps({'error': 'message and time_description required'})
    if len(message) > 1000:
        return json.dumps({'error': 'Reminder message too long (max 1000 chars)'})
    try:
        pending = ai_state._ai_db.count_pending_reminders()
        if pending >= _MAX_PENDING_REMINDERS:
            return json.dumps({'error': f'Too many pending reminders ({pending}). Delete or wait for some to fire before scheduling more (max {_MAX_PENDING_REMINDERS}).'})
    except AttributeError:
        pass
    trigger_time = _parse_time(time_desc)
    if not trigger_time:
        return json.dumps({'error': f'Could not parse time: {time_desc}'})
    reminder_id = ai_state._ai_db.add_reminder(message, trigger_time, media_type=media_type)
    dt = datetime.fromtimestamp(trigger_time, tz=TZ_LOCAL)
    media_label = {'text': 'text only', 'snapshot': 'with snapshot', 'clip': 'with 5s video clip'}
    return json.dumps({'status': 'scheduled', 'reminder_id': reminder_id, 'message': message, 'media_type': media_type, 'scheduled_for': dt.strftime('%I:%M %p, %b %d'), 'note': media_label.get(media_type, media_type)})
