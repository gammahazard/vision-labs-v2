"""
routes/bot_commands.py — Telegram bot command handlers and polling loop.

PURPOSE:
    Handles incoming Telegram updates via long-polling:
    1. Bot commands: /snapshot, /clip [N], /status, /arm, /disarm, /who, /events [N], /help
    2. Callback queries: Verdict buttons (✅ Real | ❌ False | 👤 Name)

    All incoming updates are validated via _is_authorized() before
    processing. Unauthorized users are silently ignored.

EXTRACTED FROM:
    notifications.py — to reduce file size and separate concerns.
    Bot commands (read-only + arm/disarm) are distinct from alert-sending
    functions (notify_person_detected, etc.).
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
import httpx

import routes as ctx
import routes.ai_state as ai_state
from contracts.time_rules import get_time_period
from routes.notifications import (
    is_configured, _is_authorized,
    send_text, send_photo, send_video,
    edit_message_buttons, answer_callback_query,
    get_latest_frame, build_clip, _now_str,
    TELEGRAM_API, TELEGRAM_CHAT_ID, TELEGRAM_ALLOWED_USERS,
    REDIS_HOST, REDIS_PORT,
)

logger = logging.getLogger("dashboard.notifications")

# Timezone
TZ_LOCAL = ZoneInfo(os.getenv("LOCATION_TIMEZONE", "America/Toronto"))

# Telegram update offset — tracks which updates we've processed.
# Loaded from Redis at startup so dashboard restarts don't replay old commands.
_telegram_update_offset = 0
_TELEGRAM_OFFSET_KEY = "telegram:last_offset"

# Telegram audit trail directory (per-user command logs + media)
TELEGRAM_LOG_DIR = os.environ.get("TELEGRAM_LOG_DIR", "/data/telegram")


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
    """Save media (snapshot/clip) to the per-user audit folder. Returns relative path."""
    try:
        folder_name = f"@{username}" if username else f"id_{user_id}"
        subdir = "snapshots" if media_type == "snapshot" else "clips"
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
    for uid in TELEGRAM_ALLOWED_USERS:
        meta = json.dumps({
            "chat_id": TELEGRAM_CHAT_ID,
            "name": "Admin (seeded)",
            "username": "",
            "role": "admin",
            "approved_at": datetime.now(TZ_LOCAL).strftime("%Y-%m-%d %H:%M"),
        })
        ctx.r.hset(ctx.TELEGRAM_USERS_KEY, str(uid), meta)
    logger.info(f"Seeded {len(TELEGRAM_ALLOWED_USERS)} user(s) from TELEGRAM_ALLOWED_USERS env var")


# ---------------------------------------------------------------------------
# Polling loop — runs as a background task
# ---------------------------------------------------------------------------
async def poll_telegram_callbacks():
    """
    Background task: poll Telegram for updates (commands).

    Handles incoming bot commands (/snapshot, /clip, /status, /arm, /disarm, /who).

    Security: ALL incoming updates are validated via _is_authorized() before
    processing. Unauthorized users are silently ignored.
    """
    global _telegram_update_offset

    if not is_configured():
        logger.info("Telegram not configured — callback poller disabled")
        return

    # Restore persisted offset so a dashboard restart doesn't replay updates
    # from the last 24 hours (Telegram retains unack'd updates server-side).
    try:
        saved = ctx.r.get(_TELEGRAM_OFFSET_KEY)
        if saved:
            _telegram_update_offset = int(saved)
            logger.info(f"Telegram offset restored from Redis: {_telegram_update_offset}")
    except Exception as e:
        logger.warning(f"Failed to restore Telegram offset (starting at 0): {e}")

    # Seed users from env var on first startup
    _seed_users_from_env()

    if TELEGRAM_ALLOWED_USERS:
        logger.info(f"Telegram poller started — authorized users: {TELEGRAM_ALLOWED_USERS}")
    else:
        logger.warning("Telegram poller started — NO user whitelist set (commands disabled)")

    # Register bot command menu with Telegram
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{TELEGRAM_API}/setMyCommands",
                json={"commands": [
                    {"command": "snapshot", "description": "📸 Live photo · [camera]"},
                    {"command": "clip", "description": "🎬 Video clip · [5-40s] [camera]"},
                    {"command": "status", "description": "📊 System health · [camera]"},
                    {"command": "who", "description": "👁️ Who's in frame · [camera]"},
                    {"command": "events", "description": "📋 Recent detections · [1-20] [camera]"},
                    {"command": "zones", "description": "🗺️ Zone overlays · [camera]"},
                    {"command": "analyze", "description": "👁️ AI vision · [camera] [prompt]"},
                    {"command": "cameras", "description": "📷 List configured cameras"},
                    {"command": "timelapse", "description": "⏩ Day timelapse · [YYYY-MM-DD]"},
                    {"command": "rules", "description": "📜 Notification rules"},
                    {"command": "night", "description": "🌙 Night override status"},
                    {"command": "faces", "description": "👤 Enrolled faces"},
                    {"command": "ask", "description": "🧠 Ask the AI assistant"},
                    {"command": "arm", "description": "🟢 Enable notifications (admin)"},
                    {"command": "disarm", "description": "🔴 Disable notifications (admin)"},
                    {"command": "help", "description": "ℹ️ List all commands"},
                ]},
                timeout=10,
            )
        logger.info("Telegram command menu registered")
    except Exception as e:
        logger.warning(f"Failed to register command menu: {e}")

    while True:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{TELEGRAM_API}/getUpdates",
                    params={
                        "offset": _telegram_update_offset,
                        "timeout": 30,
                        "allowed_updates": json.dumps(["callback_query", "message"]),
                    },
                    timeout=40,
                )

            if resp.status_code != 200:
                logger.warning(f"getUpdates failed: {resp.status_code}")
                await asyncio.sleep(5)
                continue

            updates = resp.json().get("result", [])
            for update in updates:
                _telegram_update_offset = update["update_id"] + 1
                # Persist after each update so a crash mid-batch is recoverable.
                # SET is cheap; one round-trip per processed update is fine.
                try:
                    ctx.r.set(_TELEGRAM_OFFSET_KEY, _telegram_update_offset)
                except Exception:
                    pass  # best-effort; in-memory offset still advances

                # --- Callback queries (verdict buttons) ---
                cb = update.get("callback_query")
                if cb:
                    cb_from = cb.get("from", {})
                    cb_user_id = cb_from.get("id")
                    cb_username = cb_from.get("username", "")
                    cb_first = cb_from.get("first_name", "")
                    cb_last = cb_from.get("last_name", "")
                    cb_lang = cb_from.get("language_code", "")
                    cb_chat_id = cb.get("message", {}).get("chat", {}).get("id")
                    authorized = _is_authorized(cb_user_id, cb_chat_id)
                    _log_access(cb_user_id, cb_username, cb_first,
                                cb_chat_id, "callback", authorized,
                                last_name=cb_last, language_code=cb_lang)
                    if not authorized:
                        logger.warning(f"Unauthorized callback from user {cb_user_id}")
                        # Emit event so it shows in the dashboard events feed
                        try:
                            ctx.r.xadd(ctx.EVENT_STREAM, {
                                "camera_id": ctx.CAMERA_ID,
                                "event_type": "unauthorized_access",
                                "timestamp": str(datetime.now().timestamp()),
                                "person_id": "",
                                "identity_name": f"{cb_first} {cb_last}".strip() or cb_username or str(cb_user_id),
                                "duration": "0",
                                "direction": "",
                                "action": "callback",
                                "bbox": "",
                                "frame_count": "0",
                                "zone": "",
                                "alert_level": "alert",
                                "alert_triggered": "True",
                                "telegram_user_id": str(cb_user_id),
                                "telegram_username": cb_username,
                                "time_period": "",
                            }, maxlen=5000)
                        except Exception:
                            pass
                        continue
                    # Inline-keyboard "pick a camera" buttons send callback_data
                    # like "cmd:snapshot:cam2" or "cmd:clip:cam2:10". Reconstitute
                    # those into a synthetic command and re-dispatch.
                    cb_data = cb.get("data", "") or ""
                    if cb_data.startswith("cmd:"):
                        parts = cb_data.split(":")
                        # parts: ["cmd", "<command>", "<camera>", "<extra>...?"]
                        if len(parts) >= 3:
                            cmd_name = "/" + parts[1]
                            cam_token = parts[2]
                            extra = " ".join(parts[3:]) if len(parts) > 3 else ""
                            synth_text = f"{cmd_name} {cam_token}".strip()
                            if extra:
                                synth_text += " " + extra
                            await answer_callback_query(cb.get("id", ""), f"📷 {cam_token}")
                            await _handle_command(
                                cmd_name,
                                chat_id=str(cb_chat_id) if cb_chat_id else "",
                                text=synth_text,
                                user_id=str(cb_user_id),
                                username=cb_username,
                            )
                            continue

                    # Default: just ack
                    await answer_callback_query(cb.get("id", ""), "OK")
                    continue

                # --- Messages (bot commands) ---
                msg = update.get("message")
                if msg:
                    msg_from = msg.get("from", {})
                    msg_user_id = msg_from.get("id")
                    msg_username = msg_from.get("username", "")
                    msg_first = msg_from.get("first_name", "")
                    msg_last = msg_from.get("last_name", "")
                    msg_lang = msg_from.get("language_code", "")
                    msg_chat_id = msg.get("chat", {}).get("id")
                    text = msg.get("text", "").strip()

                    authorized = _is_authorized(msg_user_id, msg_chat_id)
                    _log_access(msg_user_id, msg_username, msg_first,
                                msg_chat_id, text or "(empty)", authorized,
                                last_name=msg_last, language_code=msg_lang)

                    if not authorized:
                        # Silent rejection — don't reveal bot exists
                        logger.warning(f"Unauthorized command from user {msg_user_id}: {text}")
                        # Emit event so it shows in the dashboard events feed
                        try:
                            ctx.r.xadd(ctx.EVENT_STREAM, {
                                "camera_id": ctx.CAMERA_ID,
                                "event_type": "unauthorized_access",
                                "timestamp": str(datetime.now().timestamp()),
                                "person_id": "",
                                "identity_name": f"{msg_first} {msg_last}".strip() or msg_username or str(msg_user_id),
                                "duration": "0",
                                "direction": "",
                                "action": text.split()[0] if text else "(empty)",
                                "bbox": "",
                                "frame_count": "0",
                                "zone": "",
                                "alert_level": "alert",
                                "alert_triggered": "True",
                                "telegram_user_id": str(msg_user_id),
                                "telegram_username": msg_username,
                                "time_period": "",
                            }, maxlen=5000)
                        except Exception:
                            pass
                        continue

                    # Route to command handlers
                    if text.startswith("/"):
                        cmd = text.split()[0].lower().split("@")[0]  # Strip @botname
                        logger.info(f"Command from user {msg_user_id}: {cmd}")
                        await _handle_command(cmd, chat_id=str(msg_chat_id),
                                              text=text, user_id=str(msg_user_id),
                                              username=msg_username)
                    elif msg.get("photo"):
                        # User sent a photo — analyze it with MiniCPM-V
                        caption = msg.get("caption", "").strip()
                        await _handle_photo(
                            msg["photo"],
                            chat_id=str(msg_chat_id),
                            caption=caption,
                            user_id=str(msg_user_id),
                            username=msg_username,
                        )

        except httpx.ReadTimeout:
            # Normal — long poll timed out with no updates
            pass
        except Exception as e:
            logger.warning(f"Callback poller error: {e}")
            await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# Multi-camera helpers (Phase 9b)
# ---------------------------------------------------------------------------
# Telegram users specify a camera by typing its id ("cam2") or friendly name
# ("basement") anywhere in the command text. The resolver scans tokens for
# any match against the registry, strips it, and returns the remaining text
# so per-command arg parsing (duration, date, count) still works.
#
# Match priority:
#   1. exact id      (case-insensitive)
#   2. exact name    (case-insensitive)
#   3. unambiguous prefix match (>= 3 chars, single hit)
#   4. "all"         -> every enabled camera
#
# No match -> default to the dashboard's primary camera.

def _telegram_get_cameras() -> list:
    """Return enabled cameras from the registry (id, name, etc.) sorted by id."""
    try:
        raw = ctx.r.hgetall("cameras:registry") or {}
        out = []
        for cid, val in raw.items():
            try:
                entry = json.loads(val)
                if entry.get("enabled", True):
                    out.append(entry)
            except Exception:
                continue
        out.sort(key=lambda c: c.get("id", ""))
        return out
    except Exception:
        return []


def _camera_friendly_name(cam_id: str) -> str:
    """Look up a camera's display name. Falls back to the id if none."""
    for c in _telegram_get_cameras():
        if c.get("id") == cam_id:
            return c.get("name") or cam_id
    return cam_id


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
    """
    Scan `text` for a camera token. Returns (camera_ids, remaining_text).

    The remaining text still contains the leading command (/clip, /events, etc.)
    so existing per-command arg parsing (e.g., duration for /clip) keeps working.

    If no token matches, returns ([primary_cam_id], text_unchanged).
    """
    cams = _telegram_get_cameras()
    cam_ids = [c["id"] for c in cams]

    id_map = {c["id"].lower(): c["id"] for c in cams}
    name_map = {(c.get("name") or "").lower(): c["id"]
                for c in cams if c.get("name")}

    tokens = (text or "").split()
    new_tokens = []
    matched: list[str] | None = None

    for tok in tokens:
        if matched is None:
            tlow = tok.lower()
            # 1. "all"
            if tlow == "all":
                matched = cam_ids if cam_ids else None
                if matched:
                    continue
            # 2. exact id
            if tlow in id_map:
                matched = [id_map[tlow]]
                continue
            # 3. exact name
            if tlow in name_map:
                matched = [name_map[tlow]]
                continue
            # 4. unambiguous prefix (>= 3 chars)
            if len(tlow) >= 3:
                prefix_hits = set()
                for key, cid in id_map.items():
                    if key.startswith(tlow):
                        prefix_hits.add(cid)
                for key, cid in name_map.items():
                    if key.startswith(tlow):
                        prefix_hits.add(cid)
                if len(prefix_hits) == 1:
                    matched = list(prefix_hits)
                    continue
        new_tokens.append(tok)

    if matched is None:
        # Default to primary camera
        primary = os.getenv("CAMERA_ID", "") or (cam_ids[0] if cam_ids else "")
        matched = [primary] if primary else []

    return matched, " ".join(new_tokens)


# ---------------------------------------------------------------------------
# Command router
# ---------------------------------------------------------------------------
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


async def _handle_command(cmd: str, chat_id: str = "", text: str = "",
                          user_id: str = "", username: str = ""):
    """Route a bot command to the appropriate handler."""
    # Admin-only commands (system-wide, no per-camera scoping)
    admin_handlers = {
        "/arm": _cmd_arm,
        "/disarm": _cmd_disarm,
    }
    # All user commands now accept text so they can extract a camera token
    # if present. Each handler is responsible for parsing its own extra args.
    user_handlers = {
        "/snapshot": _cmd_snapshot,
        "/clip": _cmd_clip,
        "/status": _cmd_status,
        "/who": _cmd_who,
        "/zones": _cmd_zones,
        "/events": _cmd_events,
        "/timelapse": _cmd_timelapse,
        "/analyze": _cmd_analyze,
        "/ask": _cmd_ask,
        "/rules": _cmd_time_rules,
        "/night": _cmd_night,
        "/faces": _cmd_faces,
        "/cameras": _cmd_cameras,
        "/start": _cmd_help,
        "/help": _cmd_help,
    }

    try:
        # Log every command to the per-user audit trail
        _log_telegram_command(username, user_id, text or cmd)

        if cmd in admin_handlers:
            role = _get_user_role(user_id)
            if role != "admin":
                await send_text("🔒 This command is reserved for admins.", chat_id=chat_id)
                return
            await admin_handlers[cmd](chat_id=chat_id)
        elif cmd in user_handlers:
            await user_handlers[cmd](chat_id=chat_id, text=text,
                                      user_id=user_id, username=username)
        else:
            await _cmd_help(chat_id=chat_id)
    except Exception as e:
        logger.warning(f"Command {cmd} failed: {e}")
        await send_text(f"⚠️ Command failed: {e}", chat_id=chat_id)


# ---------------------------------------------------------------------------
# Bot command implementations
# ---------------------------------------------------------------------------
async def _cmd_snapshot(chat_id: str = "", text: str = "",
                        user_id: str = "", username: str = "", **kwargs):
    """Send a live camera snapshot with AI scene description.

    Examples:
      /snapshot           — interactive picker if >1 camera (else primary)
      /snapshot basement  — that camera
      /snapshot all       — one photo per enabled camera
    """
    # Bare /snapshot with multiple cameras → ask which one with buttons
    if not _user_specified_camera(text) and len(_telegram_get_cameras()) > 1:
        await _send_camera_picker(chat_id, "snapshot")
        return

    cam_ids, _ = _resolve_camera_token(text)
    if not cam_ids:
        await send_text("⚠️ No cameras configured. Add one in the dashboard.", chat_id=chat_id)
        return

    for cid in cam_ids:
        friendly = _camera_friendly_name(cid)
        frame = get_latest_frame(camera_id=cid)
        if frame:
            _save_telegram_media(username, user_id, frame, f"snapshot_{cid}", ".jpg")
            await send_photo(
                frame,
                f"📸 <b>{friendly}</b> — {_now_str()}",
                chat_id=chat_id,
            )
            # Run MiniCPM-V scene analysis in background (per camera)
            try:
                from routes.notifications import describe_scene
                desc = await describe_scene(frame, timeout=25.0)
                if desc:
                    await send_text(
                        f"👁️ <b>{friendly} — AI Analysis</b> <i>(MiniCPM-V)</i>\n\n{desc}",
                        chat_id=chat_id,
                    )
            except Exception as e:
                logger.debug(f"Snapshot AI analysis failed for {cid}: {e}")
        else:
            await send_text(f"⚠️ No frame available from <b>{friendly}</b>", chat_id=chat_id)


async def _cmd_clip(chat_id: str = "", text: str = "",
                    user_id: str = "", username: str = "", **kwargs):
    """Capture and send a video clip (5-40s, default 5) with AI analysis.

    Examples:
      /clip             — interactive picker if >1 camera (else primary 5s)
      /clip 15          — picker, will use 15s on the chosen camera
      /clip basement    — 5s on basement
      /clip 10 basement — 10s on basement (order doesn't matter)
      /clip all         — 5s clip from each enabled camera, sent in sequence
    """
    # No camera token + multiple cameras → ask which one with buttons.
    # Preserve any numeric duration the user typed by passing it through as
    # extra so the callback re-issues the command with both args set.
    if not _user_specified_camera(text) and len(_telegram_get_cameras()) > 1:
        extra = ""
        for tok in (text or "").split()[1:]:
            try:
                d = float(tok)
                if 5.0 <= d <= 40.0:
                    extra = str(int(d))
                    break
            except ValueError:
                continue
        await _send_camera_picker(chat_id, "clip", extra=extra)
        return

    cam_ids, remaining_text = _resolve_camera_token(text)
    if not cam_ids:
        await send_text("⚠️ No cameras configured.", chat_id=chat_id)
        return

    # Parse optional duration from the REMAINING text (camera token already stripped)
    duration = 5.0
    parts = remaining_text.split()
    for p in parts[1:]:  # skip the /clip command itself
        try:
            duration = max(5.0, min(40.0, float(p)))
            break
        except ValueError:
            continue

    loop = asyncio.get_running_loop()

    for cid in cam_ids:
        friendly = _camera_friendly_name(cid)
        await send_text(
            f"🎬 Recording {int(duration)}-second clip from <b>{friendly}</b>...",
            chat_id=chat_id,
        )
        clip_bytes = await loop.run_in_executor(
            None, lambda c=cid: build_clip(duration=duration, fps=10, camera_id=c)
        )
        if clip_bytes:
            _save_telegram_media(username, user_id, clip_bytes, f"clip_{cid}", ".mp4")
            await send_video(
                clip_bytes,
                f"🎬 <b>{friendly}</b> · {int(duration)}s — {_now_str()}",
                chat_id=chat_id,
            )

            # Extract frames from clip and analyze with MiniCPM-V (per camera)
            try:
                frames = await loop.run_in_executor(
                    None, lambda: _extract_clip_frames(clip_bytes, max_frames=6)
                )
                if frames:
                    desc = await _describe_scene_multi(frames, timeout=45.0)
                    if desc:
                        await send_text(
                            f"👁️ <b>{friendly} — Clip Analysis</b> "
                            f"<i>(MiniCPM-V · {len(frames)} frames)</i>\n\n{desc}",
                            chat_id=chat_id,
                        )
            except Exception as e:
                logger.debug(f"Clip AI analysis failed for {cid}: {e}")
        else:
            await send_text(
                f"⚠️ Failed to capture clip from <b>{friendly}</b> — not enough frames",
                chat_id=chat_id,
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
        r_raw = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=False)

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


async def _cmd_arm(chat_id: str = ""):
    """Enable all notifications by setting Redis config."""
    try:
        ctx.r.hset(ctx.CONFIG_KEY, mapping={
            "notify_person": "1",
            "notify_vehicle": "1",
        })
        await send_text("🟢 Notifications <b>armed</b> — person + vehicle alerts enabled.", chat_id=chat_id)
        logger.info("Notifications armed via Telegram (wrote Redis config)")
    except Exception as e:
        await send_text(f"⚠️ Failed to arm: {e}", chat_id=chat_id)


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


async def _cmd_who(chat_id: str = "", text: str = "", **kwargs):
    """Report who/what is currently in frame across one or more cameras.

    Default = all cameras (aggregated, since 'who is around right now?' is
    inherently a system-wide question). Specify a camera to narrow:
      /who           — all cameras
      /who basement  — basement only
    """
    from contracts.streams import STATE_KEY as _STATE_TMPL, stream_key as _stream_key
    try:
        cam_ids, _ = _resolve_camera_token(text)
        # Default to "all" for /who when no token was present
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

        parts = ["👁️ <b>Current Scene</b>"]
        for cid in cam_ids:
            friendly = _camera_friendly_name(cid)
            state_key = _stream_key(_STATE_TMPL, camera_id=cid)
            state = ctx.r.hgetall(state_key) or {}

            parts.append(f"\n<b>📷 {friendly}</b>")
            if not state:
                parts.append("  • no detection state — may be clear")
                continue

            num_people = int(state.get("num_people", "0") or 0)
            if num_people > 0:
                parts.append(f"  • People: {num_people}")
                try:
                    people = json.loads(state.get("people", "[]"))
                    for p in people[:5]:
                        name = p.get("identity_name", p.get("id", "unknown"))
                        action = p.get("action", "")
                        parts.append(f"    — {name}{f' ({action})' if action else ''}")
                except json.JSONDecodeError:
                    pass
            else:
                parts.append("  • People: none")

            num_vehicles = int(state.get("num_vehicles", "0") or 0)
            if num_vehicles > 0:
                parts.append(f"  • Vehicles: {num_vehicles}")
                try:
                    vehicles = json.loads(state.get("vehicles", "[]"))
                    for v in vehicles[:5]:
                        parts.append(f"    — {v.get('class', 'vehicle')}")
                except json.JSONDecodeError:
                    pass

        parts.append(f"\n🕐 {_now_str()}")
        await send_text("\n".join(parts), chat_id=chat_id)
    except Exception as e:
        await send_text(f"⚠️ Failed to read scene state: {e}", chat_id=chat_id)


async def _cmd_help(chat_id: str = "", **kwargs):
    """Send list of available commands."""
    cams = _telegram_get_cameras()
    if cams:
        cam_examples = " · ".join(
            f"<code>{c.get('name') or c.get('id')}</code>" for c in cams[:4]
        )
        multi_note = (
            "\n📷 <b>Multi-camera</b>\n"
            f"Most commands accept an optional camera name at the end:\n"
            f"  {cam_examples}\n"
            "Use <code>all</code> to fan-out across every camera.\n"
            "Examples: <code>/snapshot basement</code> · <code>/clip 10 front</code> · <code>/events all</code>\n"
            "See <code>/cameras</code> to list available.\n"
        )
    else:
        multi_note = ""

    await send_text(
        "🤖 <b>Vision Labs Bot</b>\n\n"
        "/snapshot [camera] — 📸 Live photo + AI analysis\n"
        "/clip [5-40] [camera] — 🎬 Video clip + AI analysis\n"
        "/analyze [camera] [prompt] — 👁️ AI vision analysis\n"
        "/status [camera] — 📊 System health\n"
        "/who [camera] — 👁️ Who's in frame now\n"
        "/events [1-20] [camera] — 📋 Recent detections\n"
        "/zones [camera] — 🗺️ Camera view with zones drawn\n"
        "/cameras — 📷 List configured cameras\n"
        "/rules — 📜 Time rules overview\n"
        "/night — 🌙 Night mode status\n"
        "/faces — 👤 Enrolled faces\n"
        "/timelapse [YYYY-MM-DD] [camera] — ⏩ Timelapse from snapshots\n"
        "/ask [question] — 🧠 Ask the AI assistant\n\n"
        + multi_note +
        "📷 <b>Send a photo</b> to get AI vision analysis\n\n"
        "🔒 <b>Admin Only</b>\n"
        "/arm — 🟢 Enable notifications\n"
        "/disarm — 🔴 Disable notifications",
        chat_id=chat_id,
    )


async def _cmd_cameras(chat_id: str = "", **kwargs):
    """List all registered cameras with online/offline status + detector flags."""
    from contracts.streams import FRAME_STREAM as _FRAME_TMPL, stream_key as _stream_key
    try:
        cams = _telegram_get_cameras()
        if not cams:
            await send_text("📷 No cameras configured. Add one in the dashboard.", chat_id=chat_id)
            return

        lines = ["📷 <b>Cameras</b>"]
        for c in cams:
            cid = c.get("id", "?")
            name = c.get("name") or cid
            # Check liveness via frame stream presence
            try:
                frame_stream = _stream_key(_FRAME_TMPL, camera_id=cid)
                frame_len = ctx.r.xlen(frame_stream) if frame_stream else 0
                online = "🟢" if frame_len > 0 else "⚪"
            except Exception:
                online = "❓"

            detectors = []
            if c.get("detect_persons", True): detectors.append("persons")
            if c.get("detect_vehicles", True): detectors.append("vehicles")
            if c.get("detect_faces", True): detectors.append("faces")
            det_str = ", ".join(detectors) if detectors else "none"

            lines.append(
                f"\n{online} <b>{name}</b> (<code>{cid}</code>)\n"
                f"  • Detectors: {det_str}"
            )

        lines.append(
            "\n\nUse a camera's name in any command:\n"
            "<code>/snapshot basement</code> · <code>/clip 10 front_door</code>"
        )
        await send_text("\n".join(lines), chat_id=chat_id)
    except Exception as e:
        await send_text(f"⚠️ Failed to list cameras: {e}", chat_id=chat_id)


# ---------------------------------------------------------------------------
# Vision analysis helpers — MiniCPM-V integration
# ---------------------------------------------------------------------------
def _extract_clip_frames(mp4_bytes: bytes, max_frames: int = 6) -> list[bytes]:
    """Extract evenly-spaced frames from an MP4 clip as JPEG bytes."""
    import tempfile

    tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    tmp_path = tmp.name
    try:
        tmp.write(mp4_bytes)
        tmp.close()

        cap = cv2.VideoCapture(tmp_path)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if total < 1:
            cap.release()
            return []

        # Pick evenly-spaced frame indices
        n = min(max_frames, total)
        indices = [int(i * (total - 1) / max(n - 1, 1)) for i in range(n)]

        frames = []
        for idx in sorted(set(indices)):
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ret, frame = cap.read()
            if ret:
                _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                frames.append(buf.tobytes())

        cap.release()
        return frames
    except Exception:
        return []
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


async def _describe_scene_multi(frames: list[bytes],
                                prompt: str = "",
                                timeout: float = 45.0) -> str:
    """Send multiple frames to MiniCPM-V for analysis. Returns description."""
    import re as _re

    if not prompt:
        prompt = (
            "These are frames from a security camera video clip. "
            "Describe what is happening across the clip: any people, their actions, "
            "vehicles, changes between frames, and anything notable."
        )

    def _call_vision_multi() -> str:
        try:
            import ollama as ollama_lib
            OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://ollama:11434")
            VISION_MODEL = os.getenv("VISION_MODEL", "minicpm-v")
            client = ollama_lib.Client(host=OLLAMA_HOST)
            response = client.chat(
                model=VISION_MODEL,
                messages=[{
                    "role": "user",
                    "content": prompt,
                    "images": frames,
                }],
                options={"num_predict": 300},
                keep_alive=OLLAMA_KEEP_ALIVE,
            )
            text = response.message.content.strip()
            text = _re.sub(r"<think>.*?</think>", "", text, flags=_re.DOTALL).strip()
            return text
        except Exception as e:
            logger.warning(f"Multi-frame vision analysis failed: {e}")
            return ""

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_call_vision_multi),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning(f"Multi-frame vision timed out after {timeout}s")
        return ""
    except Exception as e:
        logger.warning(f"_describe_scene_multi error: {e}")
        return ""


async def _handle_photo(photo_list: list, chat_id: str = "",
                        caption: str = "", user_id: str = "",
                        username: str = ""):
    """Download a user-sent photo and analyze it with MiniCPM-V."""
    from routes.notifications import describe_scene

    try:
        _log_telegram_command(username, user_id, f"(photo) {caption}" if caption else "(photo)")

        # Telegram sends multiple sizes — pick the largest
        photo = photo_list[-1] if photo_list else None
        if not photo:
            await send_text("⚠️ Could not read photo", chat_id=chat_id)
            return

        file_id = photo.get("file_id", "")
        if not file_id:
            await send_text("⚠️ No file_id in photo", chat_id=chat_id)
            return

        await send_text("👁️ Analyzing your photo...", chat_id=chat_id)

        # Download the photo from Telegram
        async with httpx.AsyncClient() as client:
            # Get file path
            file_resp = await client.get(
                f"{TELEGRAM_API}/getFile",
                params={"file_id": file_id},
                timeout=10,
            )
            if file_resp.status_code != 200:
                await send_text("⚠️ Failed to download photo from Telegram", chat_id=chat_id)
                return

            file_path = file_resp.json().get("result", {}).get("file_path", "")
            if not file_path:
                await send_text("⚠️ Could not get file path", chat_id=chat_id)
                return

            # Download actual file bytes
            bot_token = TELEGRAM_API.split("/bot")[1].split("/")[0] if "/bot" in TELEGRAM_API else ""
            download_url = f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
            dl_resp = await client.get(download_url, timeout=15)
            if dl_resp.status_code != 200:
                await send_text("⚠️ Failed to download photo", chat_id=chat_id)
                return

            photo_bytes = dl_resp.content

        # Save to audit trail
        _save_telegram_media(username, user_id, photo_bytes, "snapshot", ".jpg")

        # Analyze with MiniCPM-V
        prompt = caption if caption else (
            "Describe this image in detail. Include any people, objects, "
            "text, activities, and notable details."
        )
        desc = await describe_scene(photo_bytes, prompt=prompt, timeout=30.0)

        if desc:
            await send_text(f"👁️ <b>Vision Analysis</b> <i>(MiniCPM-V)</i>\n\n{desc}", chat_id=chat_id)
        else:
            await send_text("⚠️ Vision model could not analyze this photo (timeout or error)", chat_id=chat_id)

    except Exception as e:
        logger.warning(f"Photo analysis failed: {e}")
        await send_text(f"⚠️ Photo analysis failed: {e}", chat_id=chat_id)


async def _cmd_analyze(chat_id: str = "", text: str = "",
                       user_id: str = "", username: str = "", **kwargs):
    """Analyze a live camera frame with MiniCPM-V vision model.

    Examples:
      /analyze                        — primary camera, default prompt
      /analyze basement               — basement, default prompt
      /analyze is the gate open       — primary, custom prompt
      /analyze basement count people  — basement, custom prompt
      /analyze all                    — analyze each camera in turn
    """
    from routes.notifications import describe_scene

    cam_ids, remaining_text = _resolve_camera_token(text)
    if not cam_ids:
        await send_text("⚠️ No cameras configured.", chat_id=chat_id)
        return

    # Custom prompt is whatever is left in remaining_text after the command itself
    custom_prompt = remaining_text.replace("/analyze", "", 1).strip()
    default_prompt = (
        "Describe this security camera image in detail. "
        "Include: lighting/time of day, weather if visible, "
        "any people (count, appearance, actions), vehicles, "
        "and anything notable or unusual."
    )
    prompt = custom_prompt or default_prompt

    for cid in cam_ids:
        friendly = _camera_friendly_name(cid)
        frame = get_latest_frame(camera_id=cid)
        if not frame:
            await send_text(f"⚠️ No frame available from <b>{friendly}</b>", chat_id=chat_id)
            continue

        await send_text(f"👁️ Analyzing live frame from <b>{friendly}</b>...", chat_id=chat_id)
        try:
            desc = await describe_scene(frame, prompt=prompt, timeout=30.0)
            if desc:
                await send_photo(
                    frame,
                    f"📸 <b>{friendly}</b> — {_now_str()}",
                    chat_id=chat_id,
                )
                await send_text(
                    f"👁️ <b>{friendly} — Vision Analysis</b> <i>(MiniCPM-V)</i>\n\n{desc}",
                    chat_id=chat_id,
                )
            else:
                await send_text(
                    f"⚠️ Vision model timed out for <b>{friendly}</b>",
                    chat_id=chat_id,
                )
        except Exception as e:
            await send_text(f"⚠️ Analysis failed for <b>{friendly}</b>: {e}", chat_id=chat_id)


# Snapshot directory — same as server.py uses
SNAPSHOT_DIR = os.environ.get("SNAPSHOT_DIR", "/data/snapshots")


async def _cmd_events(chat_id: str = "", text: str = "", **kwargs):
    """Show recent detection events with snapshot images.

    Examples:
      /events            — last 5 from all cameras (merged)
      /events 10         — last 10 from all cameras
      /events basement   — last 5 from basement only
      /events 10 all     — last 10 across every camera
    """
    from contracts.streams import EVENT_STREAM as _EVT_TMPL, stream_key as _stream_key

    cam_ids, remaining_text = _resolve_camera_token(text)
    # /events defaults to aggregating across all cameras when no token given.
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

    # Parse count from the remaining text (camera already stripped)
    count = 5
    for p in remaining_text.split()[1:]:
        try:
            count = max(1, min(20, int(p)))
            break
        except ValueError:
            continue

    try:
        r_ev = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
        # Pull last N from each camera, merge by stream id (ms timestamp), trim to N
        merged = []
        for cid in cam_ids:
            evt_stream = _stream_key(_EVT_TMPL, camera_id=cid)
            entries = r_ev.xrevrange(evt_stream, count=count)
            for msg_id, data in entries:
                merged.append((msg_id, dict(data), cid))
        # Sort newest-first using the millisecond timestamp encoded in stream id
        def _ms(mid):
            try:
                return int(str(mid).split("-")[0])
            except Exception:
                return 0
        merged.sort(key=lambda x: _ms(x[0]), reverse=True)
        merged = merged[:count]

        if not merged:
            cams_label = ", ".join(_camera_friendly_name(c) for c in cam_ids) or "any camera"
            await send_text(f"📋 No events recorded yet on {cams_label}.", chat_id=chat_id)
            return

        cams_label = ", ".join(_camera_friendly_name(c) for c in cam_ids)
        await send_text(
            f"📋 <b>Recent Events</b> from {cams_label} (showing {len(merged)})",
            chat_id=chat_id,
        )

        for msg_id, data, src_cid in merged:
            etype = data.get("event_type", "unknown")
            identity = data.get("identity_name", "")
            person_id = data.get("person_id", "")
            zone = data.get("zone", "")
            ts_raw = data.get("timestamp", "")

            icons = {
                "person_appeared": "🚨",
                "person_identified": "👤",
                "vehicle_detected": "🚗",
                "vehicle_idle": "🚗",
            }
            icon = icons.get(etype, "📌")
            who = identity if identity else person_id if person_id else "unknown"

            # Format timestamp: convert unix float to readable time
            time_str = ""
            if ts_raw:
                try:
                    ts_float = float(ts_raw)
                    dt = datetime.fromtimestamp(ts_float, tz=TZ_LOCAL)
                    time_str = dt.strftime("%I:%M %p")
                except (ValueError, OSError):
                    time_str = ts_raw  # Fallback to raw if not a float

            # Prefix with camera name only when reporting across multiple cameras
            src_label = _camera_friendly_name(src_cid)
            cam_prefix = f"📷 {src_label} · " if len(cam_ids) > 1 else ""
            caption = f"{cam_prefix}{icon} <b>{etype.replace('_', ' ').title()}</b>"
            if who and who != "unknown":
                caption += f" — {who}"
            if zone:
                caption += f" ({zone})"
            if time_str:
                caption += f"\n🕐 {time_str}"

            # Try to send event snapshot as photo (per-camera path)
            from routes.events import resolve_event_snapshot_path
            mid = msg_id if isinstance(msg_id, str) else msg_id.decode()
            snap_path = resolve_event_snapshot_path(mid, camera_id=src_cid)
            sent_photo = False
            if snap_path and os.path.isfile(snap_path):
                try:
                    with open(snap_path, "rb") as f:
                        snap_bytes = f.read()
                    if snap_bytes:
                        await send_photo(snap_bytes, caption, chat_id=chat_id)
                        sent_photo = True
                except Exception:
                    pass

            if not sent_photo:
                await send_text(caption, chat_id=chat_id)

    except Exception as e:
        await send_text(f"⚠️ Failed to fetch events: {e}", chat_id=chat_id)


# ---------------------------------------------------------------------------
# /zones — Camera snapshot with zone overlays
# ---------------------------------------------------------------------------
async def _cmd_zones(chat_id: str = "", text: str = "", **kwargs):
    """Send a camera snapshot with all security zones drawn on it.

    Zones are per-camera, so:
      /zones            — primary camera
      /zones basement   — that camera
      /zones all        — one image per camera with its own zones drawn
    """
    from contracts.streams import ZONE_KEY as _ZONE_TMPL, stream_key as _stream_key

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


# ---------------------------------------------------------------------------
# /rules — Active suppression rules
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# /night — Night mode status
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# /faces — Enrolled faces list
# ---------------------------------------------------------------------------
async def _cmd_faces(chat_id: str = "", **kwargs):
    """List enrolled/known faces."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{ctx.FACE_API_URL}/api/faces")
            if resp.status_code != 200:
                await send_text("⚠️ Face recognizer service unavailable", chat_id=chat_id)
                return
            data = resp.json()

        faces = data.get("faces", [])
        if not faces:
            await send_text("👤 No faces enrolled yet — use the dashboard to add people.", chat_id=chat_id)
            return

        # Group by name — each photo angle is a separate DB row
        from collections import Counter
        name_counts = Counter(f.get("name", "unnamed") for f in faces)

        parts = [f"👤 <b>Enrolled Faces</b> ({len(name_counts)} people)\n"]
        for name, count in name_counts.most_common():
            parts.append(f"  • {name} ({count} photo(s))")

        await send_text("\n".join(parts), chat_id=chat_id)
    except Exception as e:
        await send_text(f"⚠️ Failed to fetch faces: {e}", chat_id=chat_id)


# ---------------------------------------------------------------------------
# /timelapse — Stitch event snapshots into MP4
# ---------------------------------------------------------------------------
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
        # Scan every camera subdir + legacy root
        subdir_globs = [
            os.path.join(SNAPSHOT_DIR, d, "*.jpg")
            for d in os.listdir(SNAPSHOT_DIR)
            if os.path.isdir(os.path.join(SNAPSHOT_DIR, d)) and d != "vehicles" and d != "clips"
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


# ---------------------------------------------------------------------------
# Callback handler — verdict buttons on notification messages
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# /ask — AI assistant via Telegram
# ---------------------------------------------------------------------------
# Ollama config — shared with the rest of dashboard via constants module
from constants import CHAT_MODEL as OLLAMA_MODEL, OLLAMA_KEEP_ALIVE, OLLAMA_HOST


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


async def _cmd_ask(chat_id: str = "", text: str = "", **kwargs):
    """Ask the local AI assistant a question via Telegram."""
    # Extract the question from the message text
    question = text[len("/ask"):].strip() if text.startswith("/ask") else text.strip()
    if not question:
        await send_text(
            "🧠 <b>Ask the AI</b>\n\n"
            "Usage: /ask [your question]\n\n"
            "Examples:\n"
            "• /ask how many people were detected today?\n"
            "• /ask what's the weather like?\n"
            "• /ask take a snapshot and describe the scene\n"
            "• /ask show me vehicle detections from today",
            chat_id=chat_id,
        )
        return

    # Send "thinking" indicator
    await send_text("🧠 Thinking...", chat_id=chat_id)

    try:
        import ollama as ollama_lib
        import routes.ai_state as ai_state
        from routes.ai_tools import TOOLS, execute_tool
        from routes.ai_prompts import build_system_context, build_system_prompt

        # Check if AI is configured
        if not ai_state._ai_db:
            await send_text("⚠️ AI assistant not initialized. Set up via the dashboard first.", chat_id=chat_id)
            return

        config = ai_state._ai_db.get_config()
        if not config.get("enabled"):
            await send_text("⚠️ AI assistant is disabled. Enable it via the dashboard.", chat_id=chat_id)
            return

        # Build system prompt with live context
        system_context = await build_system_context()
        system_prompt = build_system_prompt(config, system_context)

        # Add Telegram-specific instruction
        system_prompt += (
            "\n\nYou are replying via Telegram. Keep responses concise. "
            "Use plain text or minimal HTML formatting (<b>bold</b>, <i>italic</i>). "
            "Do NOT use markdown. Do NOT include image/video data."
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question},
        ]

        # Call Ollama
        client = ollama_lib.Client(host=OLLAMA_HOST)
        loop = asyncio.get_running_loop()

        # Per-request media tracking to avoid race conditions with web chat
        import uuid as _uuid
        request_id = _uuid.uuid4().hex
        ai_state.set_request_id(request_id)

        response = await loop.run_in_executor(
            None,
            lambda: client.chat(
                model=OLLAMA_MODEL,
                messages=messages,
                tools=TOOLS,
                options={"num_ctx": 8192},
                think=False,
                keep_alive=OLLAMA_KEEP_ALIVE,
            ),
        )

        # Handle tool calls (up to 5 rounds)
        tool_rounds = 0
        while response.message.tool_calls and tool_rounds < 5:
            tool_rounds += 1
            messages.append(response.message)

            for tool_call in response.message.tool_calls:
                tool_name = tool_call.function.name
                tool_args = tool_call.function.arguments
                logger.info(f"AI tool call (Telegram): {tool_name}({tool_args})")

                result = await execute_tool(tool_name, tool_args)
                messages.append({"role": "tool", "content": result})

            response = await loop.run_in_executor(
                None,
                lambda: client.chat(
                    model=OLLAMA_MODEL,
                    messages=messages,
                    tools=TOOLS,
                    options={"num_ctx": 8192},
                    think=False,
                    keep_alive=OLLAMA_KEEP_ALIVE,
                ),
            )

        # Extract reply
        reply = response.message.content or ""

        # Strip <think> blocks
        if "<think>" in reply:
            reply = re.sub(r"<think>.*?</think>\s*", "", reply, flags=re.DOTALL).strip()

        # Collect media stashed by tools during this request
        media = ai_state.collect_media(request_id)

        # Snapshot: send as photo to this user's chat
        if media["snapshot"]:
            try:
                import base64
                snap_bytes = base64.b64decode(media["snapshot"])
                await send_photo(snap_bytes, f"📸 AI snapshot — {_now_str()}", chat_id=chat_id)
            except Exception as e:
                logger.debug(f"Failed to send AI snapshot via Telegram: {e}")

        # Clip: read file and send as video
        if media["clip"]:
            try:
                clip_dir = os.path.join("/data/snapshots", "clips")
                clip_path = os.path.join(clip_dir, media["clip"])
                if os.path.isfile(clip_path):
                    with open(clip_path, "rb") as f:
                        clip_data = f.read()
                    await send_video(clip_data, f"🎬 AI clip — {_now_str()}", chat_id=chat_id)
            except Exception as e:
                logger.debug(f"Failed to send AI clip via Telegram: {e}")

        # Browse images (vehicle snapshots, face photos, etc.)
        if media["images"]:
            for img_info in media["images"][:10]:  # Cap at 10
                try:
                    url = img_info.get("url", "")
                    caption = img_info.get("caption", "")

                    # Base64 data URI (from show_faces, etc.)
                    if url.startswith("data:image/"):
                        import base64 as b64mod
                        # "data:image/jpeg;base64,/9j/4A..."
                        b64_data = url.split(",", 1)[1] if "," in url else ""
                        if b64_data:
                            img_bytes = b64mod.b64decode(b64_data)
                            await send_photo(img_bytes, caption or "📷", chat_id=chat_id)
                    # Vehicle snapshots: /api/browse/snapshot/{date}/{filename}
                    elif url.startswith("/api/browse/snapshot/"):
                        path_parts = url.replace("/api/browse/snapshot/", "").split("/", 1)
                        if len(path_parts) == 2:
                            date_part, fname = path_parts
                            safe_name = os.path.basename(fname)
                            snap_dir = ctx.VEHICLE_SNAPSHOT_DIR or "/data/vehicle_snapshots"
                            snap_path = os.path.join(snap_dir, date_part, safe_name)
                            if os.path.isfile(snap_path):
                                with open(snap_path, "rb") as f:
                                    await send_photo(f.read(), caption or "🚗 Vehicle", chat_id=chat_id)
                    # Event snapshots: /api/events/{event_id}/snapshot
                    elif url.startswith("/api/events/") and url.endswith("/snapshot"):
                        event_id = url.replace("/api/events/", "").replace("/snapshot", "")
                        from routes.events import resolve_event_snapshot_path
                        snap_path = resolve_event_snapshot_path(event_id)
                        if snap_path and os.path.isfile(snap_path):
                            with open(snap_path, "rb") as f:
                                await send_photo(f.read(), caption or "📸 Event", chat_id=chat_id)
                except Exception:
                    pass

        # Send the text reply
        if reply:
            await _send_long_text(reply, chat_id=chat_id)
        elif not media["snapshot"] and not media["clip"]:
            await send_text("🤔 No response from the AI. Try rephrasing.", chat_id=chat_id)

    except Exception as e:
        logger.error(f"AI ask error (Telegram): {e}")
        await send_text(f"⚠️ AI error: {e}", chat_id=chat_id)

