"""
routes/bot_commands/arm.py — Telegram command handler(s).

Extracted from the legacy monolithic bot_commands.py (Phase J modularization).
The function and any per-command helpers live together so adding/changing a
command is a single-file change. ``__init__.py`` wires this into the dispatcher.
"""



import routes as ctx

from ._shared import (
    logger,
    send_text,
)


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
