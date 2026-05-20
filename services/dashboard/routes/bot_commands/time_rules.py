"""
routes/bot_commands/time_rules.py — Telegram command handler(s).

Extracted from the legacy monolithic bot_commands.py (Phase J modularization).
The function and any per-command helpers live together so adding/changing a
command is a single-file change. ``__init__.py`` wires this into the dispatcher.
"""

import os
import re
import json
import asyncio
import logging
import glob
from datetime import datetime

import cv2
import numpy as np
import httpx

import routes as ctx
import routes.ai_state as ai_state
from contracts.time_rules import get_time_period
from contracts.tz import TZ_LOCAL

from ._shared import (
    logger,
    TELEGRAM_LOG_DIR,
    send_text, send_photo, send_video,
    edit_message_buttons, answer_callback_query,
    get_latest_frame, build_clip, _now_str,
    TELEGRAM_API, TELEGRAM_CHAT_ID, TELEGRAM_ALLOWED_USERS,
    is_configured, _is_authorized,
    _log_telegram_command, _save_telegram_media, _log_access,
    _telegram_get_cameras, _camera_friendly_name, _user_specified_camera,
    _send_camera_picker, _resolve_camera_token, _get_user_role,
    _send_long_text,
    _camreg,
)


async def _cmd_time_rules(chat_id: str = "", **kwargs):
    """List active time-based notification rules."""
    try:
        cfg = ctx.r.hgetall(ctx.CONFIG_KEY)
        parts = ["📜 <b>Notification Rules</b>\n"]
        parts.append(f"• Person notifications: {'🟢 On' if cfg.get('notify_person', '1') == '1' else '🔴 Off'}")
        parts.append(f"• Vehicle notifications: {'🟢 On' if cfg.get('notify_vehicle', '1') == '1' else '🔴 Off'}")
        parts.append(f"• Suppress known faces: {'🟢 On' if cfg.get('suppress_known', '0') == '1' else '🔴 Off'}")
        cooldown_p = cfg.get('notify_cooldown', '60')
        cooldown_v = cfg.get('vehicle_cooldown', '60')
        parts.append(f"• Person cooldown: {cooldown_p}s")
        parts.append(f"• Vehicle cooldown: {cooldown_v}s")
        parts.append(f"\n• Time: {_now_str()}")
        await send_text("\n".join(parts), chat_id=chat_id)
    except Exception as e:
        await send_text(f"⚠️ Failed to fetch rules: {e}", chat_id=chat_id)
