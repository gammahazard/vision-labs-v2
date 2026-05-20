"""
routes/bot_commands/analyze.py — Telegram command handler(s).

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

        # Refuse oversize uploads — a 20MB image would blow Ollama's
        # request and waste GPU time. Telegram's own limit is 10MB but
        # tighten to 8MB here so we fail fast before describe_scene.
        MAX_PHOTO_BYTES = 8 * 1024 * 1024
        if len(photo_bytes) > MAX_PHOTO_BYTES:
            await send_text(
                f"⚠️ Photo too large ({len(photo_bytes) // 1024 // 1024} MB). "
                f"Limit is {MAX_PHOTO_BYTES // 1024 // 1024} MB.",
                chat_id=chat_id,
            )
            return

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
