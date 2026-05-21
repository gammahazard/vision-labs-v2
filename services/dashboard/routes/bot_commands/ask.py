"""
routes/bot_commands/ask.py — Telegram command handler(s).

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
    OLLAMA_HOST, OLLAMA_MODEL, OLLAMA_KEEP_ALIVE,
)


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
                            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_part):
                                continue
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
