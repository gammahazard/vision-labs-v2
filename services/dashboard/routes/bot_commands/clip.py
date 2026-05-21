"""
routes/bot_commands/clip.py — Telegram command handler(s).

Extracted from the legacy monolithic bot_commands.py (Phase J modularization).
The function and any per-command helpers live together so adding/changing a
command is a single-file change. ``__init__.py`` wires this into the dispatcher.
"""

import asyncio



from ._shared import (
    logger,
    send_text, send_video,
    build_clip, _now_str,
    _save_telegram_media, _telegram_get_cameras, _camera_friendly_name, _user_specified_camera,
    _send_camera_picker, _resolve_camera_token,
)

# Vision-analysis helpers live in analyze.py (they originated together in the
# legacy monolithic bot_commands.py; the R3 split moved them with /analyze).
from .analyze import _extract_clip_frames, _describe_scene_multi


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
