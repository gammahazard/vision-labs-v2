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


SCHEMA = {'type': 'function', 'function': {'name': 'schedule_reminder', 'description': 'Schedule a reminder to be sent via Telegram at a specific time. Can include a snapshot or video clip captured at the scheduled time.', 'parameters': {'type': 'object', 'properties': {'message': {'type': 'string', 'description': 'The reminder message'}, 'time_description': {'type': 'string', 'description': "When to send, e.g. '10:00 PM', 'in 30 minutes', '2026-02-21T22:00:00'"}, 'media_type': {'type': 'string', 'enum': ['text', 'snapshot', 'clip'], 'description': "Type of media to include: 'text' (default), 'snapshot' (camera photo), or 'clip' (5-second video)."}}, 'required': ['message', 'time_description']}}}


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
