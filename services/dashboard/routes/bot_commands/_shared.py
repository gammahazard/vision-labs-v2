"""
routes/bot_commands/_shared.py — common helpers + constants for Telegram bot commands.

Extracted from the legacy monolithic bot_commands.py (Phase J modularization).
Each command file in this package imports from here for the helpers it needs
(camera resolution, audit logging, role lookup, message chunking). Things that
genuinely only one command uses stay in that command's file.

This module re-exports a number of names from routes.notifications so command
files only need `from ._shared import send_text, send_photo, ...` rather than
two import blocks.
"""

import os
import re
import json
import asyncio
import logging
import glob
from datetime import datetime
from zoneinfo import ZoneInfo

import cv2
import numpy as np
import redis
from contracts.redis_client import make_redis_client
import httpx

import routes as ctx
import routes.ai_state as ai_state
from contracts.time_rules import get_time_period
from contracts.tz import TZ_LOCAL  # validated single source of truth

# Notification surface — re-exported so commands don't have to import twice.
from routes.notifications import (
    is_configured, _is_authorized,
    send_text, send_photo, send_video,
    edit_message_buttons, answer_callback_query,
    get_latest_frame, build_clip, _now_str,
    TELEGRAM_API, TELEGRAM_CHAT_ID, TELEGRAM_ALLOWED_USERS,
    REDIS_HOST, REDIS_PORT,
)

# Ollama config — used by /ask (chat) and /analyze (vision).
# CHAT_MODEL is aliased to OLLAMA_MODEL to match the name used inside the
# legacy bot_commands.py the R3 split came from.
from constants import (
    OLLAMA_HOST,
    OLLAMA_KEEP_ALIVE,
    VISION_MODEL,
    CHAT_MODEL as OLLAMA_MODEL,
)

import cameras as _camreg

logger = logging.getLogger("dashboard.notifications")

# Telegram update offset — tracks which updates we've processed.
# Loaded from Redis at startup so dashboard restarts don't replay old commands.
_telegram_update_offset = 0
_TELEGRAM_OFFSET_KEY = "telegram:last_offset"

# Telegram audit trail directory (per-user command logs + media)
TELEGRAM_LOG_DIR = os.environ.get("TELEGRAM_LOG_DIR", "/data/telegram")

# Snapshot directory (must match routes/events.py — both consumers read the same tree)
SNAPSHOT_DIR = os.environ.get("SNAPSHOT_DIR", "/data/snapshots")


def _log_telegram_command(username: str, user_id: str, command: str,
                          media_path: str = ""):
    """Log a Telegram command to the per-user audit trail on QNAP."""
    try:
        folder_name = f"@{username}" if username else f"id_{user_id}"
        user_dir = os.path.join(TELEGRAM_LOG_DIR, folder_name)
        os.makedirs(user_dir, exist_ok=True)

        now = datetime.now(TZ_LOCAL)
        entry = {
            "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
            "user_id": user_id,
            "username": username,
            "command": command,
        }
        if media_path:
            entry["media"] = media_path

        log_path = os.path.join(user_dir, "commands.jsonl")
        with open(log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        logger.debug(f"Telegram audit log failed: {e}")

def _save_telegram_media(username: str, user_id: str,
                         media_bytes: bytes, media_type: str,
                         ext: str = ".jpg") -> str:
    """Save media (snapshot/clip) to the per-user audit folder. Returns relative path.

    `media_type` is one of `snapshot`, `snapshot_<cam>`, `clip`, `clip_<cam>`,
    or anything else (treated as `other`). Callers pass camera-suffixed
    types from per-camera dispatch; previously the comparison was strict
    `== "snapshot"` so every per-camera media ended up under `clips/`.
    """
    try:
        folder_name = f"@{username}" if username else f"id_{user_id}"
        # Normalize the type prefix so `snapshot_cam2` and `snapshot` both
        # land under the `snapshots/` subdir.
        if media_type.startswith("snapshot"):
            subdir = "snapshots"
        elif media_type.startswith("clip"):
            subdir = "clips"
        else:
            subdir = "other"
        media_dir = os.path.join(TELEGRAM_LOG_DIR, folder_name, subdir)
        os.makedirs(media_dir, exist_ok=True)

        now = datetime.now(TZ_LOCAL)
        fname = now.strftime(f"%Y-%m-%d_%H%M%S{ext}")
        path = os.path.join(media_dir, fname)
        with open(path, "wb") as f:
            f.write(media_bytes)
        return os.path.join(folder_name, subdir, fname)
    except Exception as e:
        logger.debug(f"Telegram media save failed: {e}")
        return ""

def _log_access(user_id, username, first_name, chat_id, action, authorized,
                last_name="", language_code=""):
    """Log an access attempt to the Redis access log stream."""
    try:
        if ctx.r and ctx.TELEGRAM_ACCESS_LOG:
            ctx.r.xadd(ctx.TELEGRAM_ACCESS_LOG, {
                "user_id": str(user_id or ""),
                "username": username or "",
                "first_name": first_name or "",
                "last_name": last_name or "",
                "language_code": language_code or "",
                "chat_id": str(chat_id or ""),
                "action": action,
                "authorized": "true" if authorized else "false",
                "timestamp": datetime.now(TZ_LOCAL).strftime("%Y-%m-%d %H:%M:%S"),
            }, maxlen=500)
    except Exception as e:
        logger.debug(f"Access log write failed: {e}")

def _seed_users_from_env():
    """On first startup, seed Redis users hash from env vars if empty."""
    if not ctx.r or not ctx.TELEGRAM_USERS_KEY:
        return
    if ctx.r.hlen(ctx.TELEGRAM_USERS_KEY) > 0:
        return  # Already has users
    if not TELEGRAM_ALLOWED_USERS:
        return
    # Admin promotion requires explicit opt-in via TELEGRAM_ADMIN_USERS.
    # Previously every user in TELEGRAM_ALLOWED_USERS was seeded as
    # "admin", which silently gave anyone in the env list /arm + /disarm
    # privileges — surprising default for what's just an allowlist.
    admin_ids_env = os.getenv("TELEGRAM_ADMIN_USERS", "")
    admin_ids = {
        u.strip() for u in admin_ids_env.split(",") if u.strip().isdigit()
    }
    for uid in TELEGRAM_ALLOWED_USERS:
        uid_str = str(uid)
        role = "admin" if uid_str in admin_ids else "user"
        meta = json.dumps({
            "chat_id": TELEGRAM_CHAT_ID,
            "name": "Admin (seeded)" if role == "admin" else "User (seeded)",
            "username": "",
            "role": role,
            "approved_at": datetime.now(TZ_LOCAL).strftime("%Y-%m-%d %H:%M"),
        })
        ctx.r.hset(ctx.TELEGRAM_USERS_KEY, uid_str, meta)
    logger.info(
        f"Seeded {len(TELEGRAM_ALLOWED_USERS)} user(s) from TELEGRAM_ALLOWED_USERS env "
        f"({len(admin_ids)} promoted to admin via TELEGRAM_ADMIN_USERS)"
    )

def _telegram_get_cameras() -> list:
    """Enabled cameras list, used for inline-keyboard picker + help text."""
    return _camreg.list_enabled_cameras()

def _camera_friendly_name(cam_id: str) -> str:
    """Camera display name (falls back to id)."""
    return _camreg.camera_friendly_name(cam_id)

def _user_specified_camera(text: str) -> bool:
    """True iff `text` contains a token that matches a known camera id/name
    (or "all"). Used to decide whether to show the tap-to-pick keyboard."""
    cams = _telegram_get_cameras()
    ids = {c["id"].lower() for c in cams}
    names = {(c.get("name") or "").lower() for c in cams if c.get("name")}
    for tok in (text or "").split():
        t = tok.lower()
        if t == "all" or t in ids or t in names:
            return True
    return False

async def _send_camera_picker(chat_id: str, command: str, extra: str = ""):
    """Send a tap-to-pick inline keyboard for the camera arg of a command.
    `command` is the bare command name (e.g. "snapshot", "clip").
    `extra` is appended to the callback so the chosen command gets the original
    non-camera args back (e.g. clip duration).
    """
    cams = _telegram_get_cameras()
    if not cams:
        await send_text("⚠️ No cameras configured.", chat_id=chat_id)
        return
    # 2 buttons per row
    rows: list[list[dict]] = []
    cur: list[dict] = []
    for c in cams:
        label = f"📷 {c.get('name') or c.get('id')}"
        data = f"cmd:{command}:{c['id']}"
        if extra:
            data += f":{extra}"
        cur.append({"text": label, "callback_data": data})
        if len(cur) == 2:
            rows.append(cur)
            cur = []
    if cur:
        rows.append(cur)
    # "All" button at the bottom for fan-out
    all_data = f"cmd:{command}:all"
    if extra:
        all_data += f":{extra}"
    rows.append([{"text": "🎞 All cameras", "callback_data": all_data}])

    await send_text(
        f"📷 <b>Which camera?</b>\nTap one to run <code>/{command}</code>"
        + (f" with <code>{extra}</code>" if extra else "") + ".",
        chat_id=chat_id,
        reply_markup={"inline_keyboard": rows},
    )

def _resolve_camera_token(text: str) -> tuple[list[str], str]:
    """Scan `text` for a camera token (id/name/prefix/'all'). Returns
    (camera_ids, remaining_text) — matched token stripped so per-command arg
    parsing runs on the remainder. Falls back to primary if no token matched.
    See cameras.find_camera_in_tokens for the match-priority spec."""
    primary = os.getenv("CAMERA_ID", "") or ctx.CAMERA_ID
    return _camreg.find_camera_in_tokens(text, primary)

def _get_user_role(user_id: str) -> str:
    """Get user role from Redis. Returns 'admin' or 'user'."""
    try:
        if ctx.r and ctx.TELEGRAM_USERS_KEY:
            raw = ctx.r.hget(ctx.TELEGRAM_USERS_KEY, user_id)
            if raw:
                meta_str = raw if isinstance(raw, str) else raw.decode()
                data = json.loads(meta_str)
                return data.get("role", "user")
    except Exception:
        pass
    return "user"

async def _send_long_text(text: str, chat_id: str = ""):
    """Send text, splitting at 4096 chars if needed (Telegram limit)."""
    MAX = 4000  # Leave some margin for parse_mode overhead
    if len(text) <= MAX:
        await send_text(text, chat_id=chat_id)
        return
    # Split on double-newline or single-newline boundaries
    while text:
        if len(text) <= MAX:
            await send_text(text, chat_id=chat_id)
            break
        # Find a good split point
        split_at = text.rfind("\n\n", 0, MAX)
        if split_at < 200:
            split_at = text.rfind("\n", 0, MAX)
        if split_at < 200:
            split_at = MAX
        await send_text(text[:split_at], chat_id=chat_id)
        text = text[split_at:].lstrip("\n")
