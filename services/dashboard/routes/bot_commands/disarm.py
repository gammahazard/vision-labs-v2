"""
routes/bot_commands/disarm.py — Telegram command handler(s).

Extracted from the legacy monolithic bot_commands.py (Phase J modularization).
The function and any per-command helpers live together so adding/changing a
command is a single-file change. ``__init__.py`` wires this into the dispatcher.
"""



import routes as ctx

from ._shared import (
    logger,
    send_text,
)


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
