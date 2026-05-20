"""
routes/bot_commands/zones.py — Telegram command handler(s).

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


async def _cmd_zones(chat_id: str = "", text: str = "", **kwargs):
    """Send a camera snapshot with all security zones drawn on it.

    Zones are per-camera, so:
      /zones            — picker (if >1 camera) or primary
      /zones basement   — that camera
      /zones all        — one image per camera with its own zones drawn
    """
    from contracts.streams import ZONE_KEY as _ZONE_TMPL, stream_key as _stream_key

    # If the user didn't name a camera and we have more than one configured,
    # surface the same inline-keyboard picker that /snapshot and /clip use —
    # zones are per-camera so "which camera" is the most useful disambiguation.
    if not _user_specified_camera(text) and len(_telegram_get_cameras()) > 1:
        await _send_camera_picker(chat_id, "zones")
        return

    cam_ids, _ = _resolve_camera_token(text)
    if not cam_ids:
        await send_text("⚠️ No cameras configured.", chat_id=chat_id)
        return

    for cid in cam_ids:
        friendly = _camera_friendly_name(cid)
        await _send_zones_for_camera(chat_id=chat_id, cam_id=cid, friendly=friendly)

async def _send_zones_for_camera(chat_id: str, cam_id: str, friendly: str):
    """Render a single camera's snapshot with its zones drawn on it."""
    from contracts.streams import ZONE_KEY as _ZONE_TMPL, stream_key as _stream_key
    try:
        frame_bytes = get_latest_frame(camera_id=cam_id)
        if not frame_bytes:
            await send_text(f"⚠️ No frame available from <b>{friendly}</b>", chat_id=chat_id)
            return

        zone_key = _stream_key(_ZONE_TMPL, camera_id=cam_id)
        zone_data = ctx.r.hgetall(zone_key) or {}
        if not zone_data:
            await send_photo(
                frame_bytes,
                f"🗺️ <b>{friendly}</b> — no zones defined yet. Use the dashboard to create some.",
                chat_id=chat_id,
            )
            return

        nparr = np.frombuffer(frame_bytes, np.uint8)
        frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if frame is None:
            await send_text(f"⚠️ Failed to decode <b>{friendly}</b> frame", chat_id=chat_id)
            return

        h, w = frame.shape[:2]
        zone_count = 0

        # Draw each zone (same logic as server.py WebSocket overlay)
        for zone_id, zone_json in zone_data.items():
            try:
                zone = json.loads(zone_json)
                pts_norm = zone.get("points", [])
                if len(pts_norm) < 3:
                    continue

                pts = np.array(
                    [[int(p[0] * w), int(p[1] * h)] for p in pts_norm],
                    dtype=np.int32,
                )

                # Zone color by alert level (BGR)
                alert_level = zone.get("alert_level", "log_only")
                zone_colors = {
                    "always": (0, 0, 220),        # Red
                    "night_only": (0, 140, 255),   # Orange
                    "log_only": (200, 160, 60),    # Blue
                    "ignore": (100, 100, 100),     # Gray
                    "dead_zone": (40, 40, 40),     # Dark gray
                }
                color = zone_colors.get(alert_level, (200, 160, 60))

                # Semi-transparent fill
                overlay = frame.copy()
                cv2.fillPoly(overlay, [pts], color)
                cv2.addWeighted(overlay, 0.2, frame, 0.8, 0, frame)

                # Zone border
                cv2.polylines(frame, [pts], True, color, 2)

                # Zone name label
                name = zone.get("name", zone_id)
                level_tag = alert_level.replace("_", " ").title()
                label = f"{name} [{level_tag}]"
                cx = int(np.mean(pts[:, 0]))
                cy = int(np.mean(pts[:, 1]))
                label_size = cv2.getTextSize(
                    label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1
                )[0]
                cv2.rectangle(
                    frame,
                    (cx - label_size[0] // 2 - 4, cy - label_size[1] // 2 - 4),
                    (cx + label_size[0] // 2 + 4, cy + label_size[1] // 2 + 4),
                    color, -1,
                )
                cv2.putText(
                    frame, label,
                    (cx - label_size[0] // 2, cy + label_size[1] // 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1,
                )
                zone_count += 1
            except Exception:
                continue

        # Encode back to JPEG
        _, jpeg_buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
        annotated_bytes = jpeg_buf.tobytes()

        await send_photo(
            annotated_bytes,
            f"🗺️ <b>{friendly} — Zones</b> ({zone_count} drawn)\n"
            f"🕐 {_now_str()}",
            chat_id=chat_id,
        )
    except Exception as e:
        await send_text(f"⚠️ Failed to render zones for <b>{friendly}</b>: {e}", chat_id=chat_id)
