"""
routes/bot_commands/cameras.py — Telegram command handler(s).

Extracted from the legacy monolithic bot_commands.py (Phase J modularization).
The function and any per-command helpers live together so adding/changing a
command is a single-file change. ``__init__.py`` wires this into the dispatcher.
"""



import routes as ctx

from ._shared import (
    send_text, _telegram_get_cameras,
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
            "<code>/snapshot basement</code> · <code>/clip 10 cam1</code>"
        )
        await send_text("\n".join(lines), chat_id=chat_id)
    except Exception as e:
        await send_text(f"⚠️ Failed to list cameras: {e}", chat_id=chat_id)
