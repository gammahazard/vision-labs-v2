"""
routes/bot_commands/_poller.py — background task that long-polls Telegram for updates.

Validates each incoming message/callback via _is_authorized() before
dispatching. Unauthorized attempts emit an `unauthorized_access` event so they
show up in the dashboard events feed.
"""

import os
import json
import asyncio
import logging
from datetime import datetime

import httpx

import routes as ctx
from contracts.tz import TZ_LOCAL

from ._shared import (
    logger,
    TELEGRAM_API, TELEGRAM_ALLOWED_USERS,
    is_configured, _is_authorized,
    answer_callback_query,
    _log_access, _seed_users_from_env,
    _TELEGRAM_OFFSET_KEY,
)
from ._dispatch import _handle_command
from .analyze import _handle_photo

# Module-level offset (rebound inside the poller loop).
_telegram_update_offset = 0


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
                        # Silent rejection — don't reveal bot exists. Don't
                        # log the full message text — an unauthenticated
                        # probe could paste secrets / PII to test the bot
                        # and we'd persist them. Log first token + length.
                        first_token = (text.split(maxsplit=1) or [""])[0][:40]
                        logger.warning(
                            f"Unauthorized command from user {msg_user_id}: "
                            f"first_token={first_token!r} len={len(text)}"
                        )
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
