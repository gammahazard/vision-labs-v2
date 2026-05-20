"""
routes/bot_commands/disarm.py — Telegram command handler(s).

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


async def _cmd_disarm(chat_id: str = ""):
    """Disable all notifications by setting Redis config."""
    try:
        ctx.r.hset(ctx.CONFIG_KEY, mapping={
            "notify_person": "0",
            "notify_vehicle": "0",
        })
        await send_text("🔴 Notifications <b>disarmed</b> — all alerts paused until you /arm again.", chat_id=chat_id)
        logger.info("Notifications disarmed via Telegram (wrote Redis config)")
    except Exception as e:
        await send_text(f"⚠️ Failed to disarm: {e}", chat_id=chat_id)
