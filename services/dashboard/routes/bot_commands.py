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
                    {"command": "snapshot", "description": "Live camera photo"},
                    {"command": "clip", "description": "Video clip (5-40s)"},
                    {"command": "status", "description": "System health"},
                    {"command": "ask", "description": "Ask the AI assistant"},
                    {"command": "arm", "description": "Enable notifications (admin)"},
                    {"command": "disarm", "description": "Disable notifications (admin)"},
                    {"command": "who", "description": "Who's in frame now"},
                    {"command": "events", "description": "Recent detections (1-20)"},
                    {"command": "zones", "description": "Snapshot with zone overlays"},
                    {"command": "rules", "description": "Time rules overview"},
                    {"command": "night", "description": "Night override status"},
                    {"command": "faces", "description": "Enrolled faces list"},
                    {"command": "timelapse", "description": "Day timelapse [YYYY-MM-DD]"},
                    {"command": "analyze", "description": "AI vision analysis of live frame"},
                    {"command": "help", "description": "List all commands"},
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
                    # Answer the callback query with a simple acknowledgement
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
    # Commands that accept args from the raw text
    args_handlers = {
        "/clip": _cmd_clip,
        "/events": _cmd_events,
        "/ask": _cmd_ask,
        "/timelapse": _cmd_timelapse,
        "/analyze": _cmd_analyze,
    }
    # Admin-only commands
    admin_handlers = {
        "/arm": _cmd_arm,
        "/disarm": _cmd_disarm,
    }
    # Simple commands
    simple_handlers = {
        "/snapshot": _cmd_snapshot,
        "/status": _cmd_status,
        "/who": _cmd_who,
        "/zones": _cmd_zones,
        "/rules": _cmd_time_rules,
        "/night": _cmd_night,
        "/faces": _cmd_faces,
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
        elif cmd in args_handlers:
            await args_handlers[cmd](chat_id=chat_id, text=text,
                                     user_id=user_id, username=username)
        elif cmd in simple_handlers:
            await simple_handlers[cmd](chat_id=chat_id,
                                       user_id=user_id, username=username)
        else:
            await _cmd_help(chat_id=chat_id)
    except Exception as e:
        logger.warning(f"Command {cmd} failed: {e}")
        await send_text(f"⚠️ Command failed: {e}", chat_id=chat_id)


# ---------------------------------------------------------------------------
# Bot command implementations
# ---------------------------------------------------------------------------
async def _cmd_snapshot(chat_id: str = "", user_id: str = "",
                        username: str = "", **kwargs):
    """Send a live camera snapshot with AI scene description."""
    frame = get_latest_frame()
    if frame:
        # Save copy to per-user audit trail on QNAP
        _save_telegram_media(username, user_id, frame, "snapshot", ".jpg")
        await send_photo(frame, f"📸 Live snapshot — {_now_str()}", chat_id=chat_id)

        # Run MiniCPM-V scene analysis in background
        try:
            from routes.notifications import describe_scene
            desc = await describe_scene(frame, timeout=25.0)
            if desc:
                await send_text(f"👁️ <b>AI Analysis</b> <i>(MiniCPM-V)</i>\n\n{desc}", chat_id=chat_id)
        except Exception as e:
            logger.debug(f"Snapshot AI analysis failed: {e}")
    else:
        await send_text("⚠️ No camera frame available", chat_id=chat_id)


async def _cmd_clip(chat_id: str = "", text: str = "",
                    user_id: str = "", username: str = "", **kwargs):
    """Capture and send a video clip (5-40s, default 5) with AI analysis."""
    # Parse optional duration from text: /clip 15
    duration = 5.0
    parts = text.split()
    if len(parts) >= 2:
        try:
            duration = float(parts[1])
            duration = max(5.0, min(40.0, duration))
        except (ValueError, IndexError):
            pass

    await send_text(f"🎬 Recording {int(duration)}-second clip...", chat_id=chat_id)
    loop = asyncio.get_running_loop()
    clip_bytes = await loop.run_in_executor(
        None, lambda: build_clip(duration=duration, fps=10)
    )
    if clip_bytes:
        # Save copy to per-user audit trail on QNAP
        _save_telegram_media(username, user_id, clip_bytes, "clip", ".mp4")
        await send_video(clip_bytes, f"🎬 {int(duration)}s clip — {_now_str()}", chat_id=chat_id)

        # Extract frames from clip and analyze with MiniCPM-V
        try:
            frames = await loop.run_in_executor(
                None, lambda: _extract_clip_frames(clip_bytes, max_frames=6)
            )
            if frames:
                desc = await _describe_scene_multi(frames, timeout=45.0)
                if desc:
                    await send_text(f"👁️ <b>AI Clip Analysis</b> <i>(MiniCPM-V · {len(frames)} frames)</i>\n\n{desc}", chat_id=chat_id)
        except Exception as e:
            logger.debug(f"Clip AI analysis failed: {e}")
    else:
        await send_text("⚠️ Failed to capture clip — not enough frames", chat_id=chat_id)


async def _cmd_status(chat_id: str = "", **kwargs):
    """Send system health summary."""
    try:
        r = ctx.r  # Use the centralized Redis connection
        info = r.info("memory")
        mem_used = info.get("used_memory_human", "?")

        # Check frame stream health
        frame_len = r.xlen(ctx.FRAME_STREAM) if ctx.FRAME_STREAM else 0
        # HD frame check needs raw bytes connection
        r_raw = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=False)
        hd_exists = bool(r_raw.get(ctx.HD_FRAME_KEY.encode())) if ctx.HD_FRAME_KEY else False

        # Check event stream length
        event_len = r.xlen(ctx.EVENT_STREAM)

        # Read notification preferences from Redis config
        cfg = r.hgetall(ctx.CONFIG_KEY)
        person_on = cfg.get("notify_person", "1") == "1"
        vehicle_on = cfg.get("notify_vehicle", "1") == "1"
        if person_on and vehicle_on:
            alert_str = "🟢 All alerts on"
        elif not person_on and not vehicle_on:
            alert_str = "🔴 All alerts off"
        else:
            parts_a = []
            if person_on: parts_a.append("Person")
            if vehicle_on: parts_a.append("Vehicle")
            alert_str = f"🟡 {', '.join(parts_a)} only"

        status = (
            f"📊 <b>System Status</b>\n"
            f"• Notifications: {alert_str}\n"
            f"• Redis memory: {mem_used}\n"
            f"• Frame buffer: {frame_len} frames\n"
            f"• HD stream: {'✅' if hd_exists else '❌'}\n"
            f"• Events total: {event_len}\n"
            f"• Time: {_now_str()}"
        )
        await send_text(status, chat_id=chat_id)
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


async def _cmd_who(chat_id: str = "", **kwargs):
    """Report who/what is currently in the camera frame."""
    try:
        state = ctx.r.hgetall(ctx.STATE_KEY)
        if not state:
            await send_text("👀 No detection state available — scene may be clear.", chat_id=chat_id)
            return

        parts = ["👁️ <b>Current Scene</b>"]

        # People
        num_people = int(state.get("num_people", "0"))
        if num_people > 0:
            parts.append(f"• People: {num_people}")
            try:
                people = json.loads(state.get("people", "[]"))
                for p in people[:5]:
                    name = p.get("identity_name", p.get("id", "unknown"))
                    action = p.get("action", "")
                    parts.append(f"  — {name}{f' ({action})' if action else ''}")
            except json.JSONDecodeError:
                pass
        else:
            parts.append("• People: none")

        # Vehicles (check if tracker publishes vehicle info)
        num_vehicles = int(state.get("num_vehicles", "0"))
        if num_vehicles > 0:
            parts.append(f"• Vehicles: {num_vehicles}")
            try:
                vehicles = json.loads(state.get("vehicles", "[]"))
                for v in vehicles[:5]:
                    parts.append(f"  — {v.get('class', 'vehicle')}")
            except json.JSONDecodeError:
                pass
        else:
            parts.append("• Vehicles: none")

        parts.append(f"• Time: {_now_str()}")
        await send_text("\n".join(parts), chat_id=chat_id)
    except Exception as e:
        await send_text(f"⚠️ Failed to read scene state: {e}", chat_id=chat_id)


async def _cmd_help(chat_id: str = "", **kwargs):
    """Send list of available commands."""
    await send_text(
        "🤖 <b>Vision Labs Bot</b>\n\n"
        "/snapshot — 📸 Live photo + AI analysis\n"
        "/clip [5-40] — 🎬 Video clip + AI analysis\n"
        "/analyze — 👁️ AI vision analysis of live frame\n"
        "/status — 📊 System health\n"
        "/who — 👁️ Who's in frame now\n"
        "/events [1-20] — 📋 Recent detections\n"
        "/zones — 🗺️ Camera view with zones drawn\n"
        "/rules — 📜 Time rules overview\n"
        "/night — 🌙 Night mode status\n"
        "/faces — 👤 Enrolled faces\n"
        "/timelapse [YYYY-MM-DD] — ⏩ Timelapse from snapshots\n"
        "/ask [question] — 🧠 Ask the AI assistant\n\n"
        "📷 <b>Send a photo</b> to get AI vision analysis\n\n"
        "🔒 <b>Admin Only</b>\n"
        "/arm — 🟢 Enable notifications\n"
        "/disarm — 🔴 Disable notifications",
        chat_id=chat_id,
    )


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
                keep_alive="5m",
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
    """Analyze the live camera frame with MiniCPM-V vision model."""
    from routes.notifications import describe_scene

    frame = get_latest_frame()
    if not frame:
        await send_text("⚠️ No camera frame available", chat_id=chat_id)
        return

    await send_text("👁️ Analyzing live frame...", chat_id=chat_id)

    # Use text after /analyze as custom prompt, otherwise default
    custom_prompt = text.replace("/analyze", "", 1).strip() if text else ""
    prompt = custom_prompt or (
        "Describe this security camera image in detail. "
        "Include: lighting/time of day, weather if visible, "
        "any people (count, appearance, actions), vehicles, "
        "and anything notable or unusual."
    )

    try:
        desc = await describe_scene(frame, prompt=prompt, timeout=30.0)
        if desc:
            # Also send the snapshot so they can see what was analyzed
            await send_photo(frame, f"📸 Live frame — {_now_str()}", chat_id=chat_id)
            await send_text(f"👁️ <b>AI Vision Analysis</b> <i>(MiniCPM-V)</i>\n\n{desc}", chat_id=chat_id)
        else:
            await send_text("⚠️ Vision model timed out or returned empty", chat_id=chat_id)
    except Exception as e:
        await send_text(f"⚠️ Analysis failed: {e}", chat_id=chat_id)


# Snapshot directory — same as server.py uses
SNAPSHOT_DIR = os.environ.get("SNAPSHOT_DIR", "/data/snapshots")


async def _cmd_events(chat_id: str = "", text: str = "", **kwargs):
    """Show recent detection events with snapshot images."""
    # Parse optional count from text: /events 10
    count = 5
    parts_args = text.split()
    if len(parts_args) >= 2:
        try:
            count = int(parts_args[1])
            count = max(1, min(20, count))
        except (ValueError, IndexError):
            pass

    try:
        r_ev = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
        entries = r_ev.xrevrange(ctx.EVENT_STREAM, count=count)
        if not entries:
            await send_text("📋 No events recorded yet.", chat_id=chat_id)
            return

        await send_text(f"📋 <b>Recent Events</b> (showing {len(entries)})", chat_id=chat_id)

        for msg_id, data in entries:
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

            caption = f"{icon} <b>{etype.replace('_', ' ').title()}</b>"
            if who and who != "unknown":
                caption += f" — {who}"
            if zone:
                caption += f" ({zone})"
            if time_str:
                caption += f"\n🕐 {time_str}"

            # Try to send event snapshot as photo
            safe_id = msg_id.replace(":", "-") if isinstance(msg_id, str) else msg_id.decode().replace(":", "-")
            snap_path = os.path.join(SNAPSHOT_DIR, f"{safe_id}.jpg")
            sent_photo = False
            if os.path.isfile(snap_path):
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
async def _cmd_zones(chat_id: str = "", **kwargs):
    """Send a camera snapshot with all security zones drawn on it."""
    try:
        # Get live frame
        frame_bytes = get_latest_frame()
        if not frame_bytes:
            await send_text("⚠️ No camera frame available", chat_id=chat_id)
            return

        # Load zones from Redis
        zone_data = ctx.r.hgetall(ctx.ZONE_KEY) if ctx.ZONE_KEY else {}
        if not zone_data:
            # No zones — just send the snapshot with a note
            await send_photo(frame_bytes, "🗺️ No zones defined yet — use the dashboard to create zones.", chat_id=chat_id)
            return

        # Decode frame
        nparr = np.frombuffer(frame_bytes, np.uint8)
        frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if frame is None:
            await send_text("⚠️ Failed to decode camera frame", chat_id=chat_id)
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
            f"🗺️ <b>Security Zones</b> — {zone_count} zone(s) drawn\n"
            f"🕐 {_now_str()}",
            chat_id=chat_id,
        )
    except Exception as e:
        await send_text(f"⚠️ Failed to render zones: {e}", chat_id=chat_id)


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
    """Stitch today's (or a given date's) event snapshots into a timelapse MP4."""
    # Parse optional date: /timelapse 2026-02-21
    parts_args = text.split()
    if len(parts_args) >= 2:
        date_str = parts_args[1]
    else:
        date_str = datetime.now(TZ_LOCAL).strftime("%Y-%m-%d")

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

    all_jpgs = glob.glob(os.path.join(SNAPSHOT_DIR, "*.jpg"))
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
# Ollama config (same as ai.py)
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://ollama:11434")
OLLAMA_MODEL = "qwen3:14b"


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
                keep_alive="5m",
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
                    keep_alive="5m",
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
                        safe_id = event_id.replace(":", "-")
                        snap_path = os.path.join(SNAPSHOT_DIR, f"{safe_id}.jpg")
                        if os.path.isfile(snap_path):
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

