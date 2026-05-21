"""
routes/bot_commands/snapshot.py — Telegram command handler(s).

Extracted from the legacy monolithic bot_commands.py (Phase J modularization).
The function and any per-command helpers live together so adding/changing a
command is a single-file change. ``__init__.py`` wires this into the dispatcher.
"""




from ._shared import (
    logger,
    send_text, send_photo, get_latest_frame, _now_str,
    _save_telegram_media, _telegram_get_cameras, _camera_friendly_name, _user_specified_camera,
    _send_camera_picker, _resolve_camera_token,
)


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
