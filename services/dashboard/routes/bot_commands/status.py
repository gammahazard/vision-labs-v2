"""
routes/bot_commands/status.py — Telegram command handler(s).

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


async def _cmd_status(chat_id: str = "", text: str = "", **kwargs):
    """Send system health summary. Multi-camera aware.

    Defaults to showing all cameras. Pass a camera token to scope to one:
      /status            — all cameras
      /status basement   — basement only
    """
    from contracts.streams import (
        FRAME_STREAM as _FRAME_TMPL,
        EVENT_STREAM as _EVT_TMPL,
        HD_FRAME_KEY as _HD_TMPL,
        CONFIG_KEY as _CFG_TMPL,
        stream_key as _stream_key,
    )
    try:
        r = ctx.r
        r_raw = make_redis_client(decode_responses=False, host=REDIS_HOST, port=REDIS_PORT)

        # Default to "all" for status: if user didn't name a camera, show every one.
        cam_ids, _ = _resolve_camera_token(text)
        # If user didn't include a camera token, _resolve returns [primary] —
        # for /status we override that to "all" so we always show the whole system.
        token_present = any(
            tok.lower() in (
                {c["id"].lower() for c in _telegram_get_cameras()} |
                {(c.get("name") or "").lower() for c in _telegram_get_cameras()} |
                {"all"}
            )
            for tok in text.split()
        )
        if not token_present:
            cam_ids = [c["id"] for c in _telegram_get_cameras()] or cam_ids

        info = r.info("memory")
        mem_used = info.get("used_memory_human", "?")

        parts = [f"📊 <b>System Status</b>", f"• Redis memory: {mem_used}"]

        # Per-camera health
        for cid in cam_ids:
            friendly = _camera_friendly_name(cid)
            frame_stream = _stream_key(_FRAME_TMPL, camera_id=cid)
            evt_stream = _stream_key(_EVT_TMPL, camera_id=cid)
            hd_key = _stream_key(_HD_TMPL, camera_id=cid)
            cfg_key = _stream_key(_CFG_TMPL, camera_id=cid)

            frame_len = r.xlen(frame_stream)
            event_len = r.xlen(evt_stream)
            hd_exists = bool(r_raw.get(hd_key.encode()))

            cfg = r.hgetall(cfg_key)
            person_on = cfg.get("notify_person", "1") == "1"
            vehicle_on = cfg.get("notify_vehicle", "1") == "1"
            if person_on and vehicle_on:
                alert_str = "🟢 all alerts on"
            elif not person_on and not vehicle_on:
                alert_str = "🔴 all alerts off"
            else:
                a = []
                if person_on: a.append("person")
                if vehicle_on: a.append("vehicle")
                alert_str = f"🟡 {', '.join(a)} only"

            parts.append(
                f"\n<b>📷 {friendly}</b> ({cid})\n"
                f"• Notifications: {alert_str}\n"
                f"• Frame buffer: {frame_len} frames · HD: {'✅' if hd_exists else '❌'}\n"
                f"• Events total: {event_len}"
            )

        parts.append(f"\n• Time: {_now_str()}")
        await send_text("\n".join(parts), chat_id=chat_id)
    except Exception as e:
        await send_text(f"⚠️ Status check failed: {e}", chat_id=chat_id)
