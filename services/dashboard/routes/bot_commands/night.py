"""
routes/bot_commands/night.py — Telegram command handler(s).

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


async def _cmd_night(chat_id: str = "", **kwargs):
    """Show current time period and whether night override is active."""
    try:
        now = datetime.now(TZ_LOCAL)
        period = get_time_period(now)
        is_night = period in ("night", "late_night")

        period_icons = {
            "daytime": "☀️",
            "twilight": "🌅",
            "night": "🌙",
            "late_night": "🌑",
        }
        icon = period_icons.get(period, "🕐")
        label = period.replace("_", " ").title()

        if is_night:
            msg = (
                f"{icon} <b>{label}</b>\n\n"
                f"👁️ <b>Night Override ACTIVE</b>\n"
                f"All suppression rules are bypassed.\n"
                f"Every person detection will trigger a notification.\n"
                f"Dead zones still enforced.\n\n"
                f"🕐 {now.strftime('%I:%M %p')}"
            )
        else:
            msg = (
                f"{icon} <b>{label}</b>\n\n"
                f"🔇 Night Override: OFF\n"
                f"Suppression rules are active as normal.\n\n"
                f"🕐 {now.strftime('%I:%M %p')}"
            )

        await send_text(msg, chat_id=chat_id)
    except Exception as e:
        await send_text(f"⚠️ Failed to check night status: {e}", chat_id=chat_id)
