"""
routes/bot_commands/timelapse.py — Telegram command handler(s).

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


async def _cmd_timelapse(chat_id: str = "", text: str = "", **kwargs):
    """Stitch event snapshots from a date into a timelapse MP4.

    Storage caveat (until event poller fan-out lands): the event poller only
    watches the primary camera's event stream, so disk snapshots only exist
    for primary right now. The `camera` arg is accepted but a warning is
    sent if the user asks for a non-primary camera.

    Examples:
      /timelapse                    — today (primary camera; only cam with disk snapshots)
      /timelapse 2026-02-21         — that date
      /timelapse basement           — warns: not yet on disk
    """
    cam_ids, remaining_text = _resolve_camera_token(text)
    parts_args = remaining_text.split()

    # Pull date from remaining tokens (skipping the /timelapse command itself)
    date_str = datetime.now(TZ_LOCAL).strftime("%Y-%m-%d")
    for p in parts_args[1:]:
        if re.match(r"^\d{4}-\d{2}-\d{2}$", p):
            date_str = p
            break

    # If user explicitly asked for a non-primary camera, warn — disk persistence
    # for non-primary cameras isn't implemented yet (event poller fan-out pending).
    primary_id = os.getenv("CAMERA_ID", "")
    token_specified = any(
        tok.lower() in (
            {c["id"].lower() for c in _telegram_get_cameras()} |
            {(c.get("name") or "").lower() for c in _telegram_get_cameras()} |
            {"all"}
        )
        for tok in text.split()
    )
    if token_specified and cam_ids and cam_ids[0] != primary_id:
        friendly = _camera_friendly_name(cam_ids[0])
        await send_text(
            f"ℹ️ Note: event snapshots aren't persisted to disk for "
            f"<b>{friendly}</b> yet (event poller currently watches the primary "
            f"camera only). Falling back to primary camera frames.",
            chat_id=chat_id,
        )

    if not os.path.isdir(SNAPSHOT_DIR):
        await send_text(
            f"📂 Snapshot directory not found.\n"
            f"Usage: /timelapse [YYYY-MM-DD]",
            chat_id=chat_id,
        )
        return

    # Snapshots are saved flat as {redis_id}.jpg where redis_id is like
    # "1708567891234-0" (millisecond timestamp). Parse and filter by date.
    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        await send_text(
            f"⚠️ Invalid date format: {date_str}\n"
            f"Usage: /timelapse YYYY-MM-DD",
            chat_id=chat_id,
        )
        return

    # Search per-camera subdirs. Specified camera_id narrows; otherwise scans
    # all subdirs (and the legacy flat root for pre-fan-out snapshots).
    if cam_ids and len(cam_ids) == 1 and cam_ids[0] != "all":
        search_globs = [os.path.join(SNAPSHOT_DIR, cam_ids[0], "*.jpg")]
    else:
        # Scan every camera subdir + legacy root. Guard the listdir —
        # on a fresh install or snapshot-volume mishap the dir may not
        # exist, and an unhandled OSError used to crash the command.
        try:
            entries = os.listdir(SNAPSHOT_DIR)
        except OSError as e:
            logger.warning(f"Timelapse: cannot list {SNAPSHOT_DIR}: {e}")
            entries = []
        subdir_globs = [
            os.path.join(SNAPSHOT_DIR, d, "*.jpg")
            for d in entries
            if os.path.isdir(os.path.join(SNAPSHOT_DIR, d)) and d not in ("vehicles", "clips")
        ]
        search_globs = subdir_globs + [os.path.join(SNAPSHOT_DIR, "*.jpg")]

    all_jpgs = []
    for g in search_globs:
        all_jpgs.extend(glob.glob(g))

    matching = []
    for path in all_jpgs:
        fname = os.path.splitext(os.path.basename(path))[0]  # e.g. "1708567891234-0"
        try:
            # Redis stream ID: "{ms_timestamp}-{seq}"
            ms_str = fname.split("-")[0]
            ts = datetime.fromtimestamp(int(ms_str) / 1000, tz=TZ_LOCAL)
            if ts.date() == target_date:
                matching.append((ts, path))
        except (ValueError, IndexError, OSError):
            continue

    matching.sort(key=lambda x: x[0])
    jpg_files = [p for _, p in matching]

    if len(jpg_files) < 3:
        await send_text(
            f"📂 Only {len(jpg_files)} snapshot(s) for {date_str} — need at least 3 for a timelapse.",
            chat_id=chat_id,
        )
        return

    await send_text(
        f"⏩ Building timelapse from {len(jpg_files)} snapshots ({date_str})...",
        chat_id=chat_id,
    )

    loop = asyncio.get_running_loop()
    mp4_bytes = await loop.run_in_executor(None, lambda: _build_timelapse(jpg_files))

    if mp4_bytes:
        await send_video(
            mp4_bytes,
            f"⏩ Timelapse — {date_str} ({len(jpg_files)} frames)",
            chat_id=chat_id,
        )
    else:
        await send_text("⚠️ Failed to build timelapse — encoding error", chat_id=chat_id)

def _build_timelapse(jpg_paths: list[str], fps: int = 3) -> bytes | None:
    """Stitch JPEG files into an MP4 timelapse. Returns MP4 bytes or None."""
    import tempfile

    if not jpg_paths:
        return None

    # Read first frame to get dimensions
    first = cv2.imread(jpg_paths[0])
    if first is None:
        return None
    h, w = first.shape[:2]

    # Write to a temp file
    tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    tmp_path = tmp.name
    tmp.close()

    try:
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(tmp_path, fourcc, fps, (w, h))

        for path in jpg_paths:
            img = cv2.imread(path)
            if img is None:
                continue
            # Resize if dimensions don't match first frame
            if img.shape[:2] != (h, w):
                img = cv2.resize(img, (w, h))
            # Hold each frame for a beat (repeat 2x for readability)
            writer.write(img)
            writer.write(img)
        writer.release()

        with open(tmp_path, "rb") as f:
            return f.read()
    except Exception:
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
