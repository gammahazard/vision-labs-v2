"""
routes/bot_commands/_dispatch.py — central command router.

Routes a /command string from the Telegram poller to the matching handler in
this package. Admin-only commands (arm/disarm) require the user to have the
"admin" role in Redis (cf. helpers/users.py).
"""

import logging

from ._shared import (
    logger,
    send_text,
    _log_telegram_command,
    _get_user_role,
)
from .snapshot import _cmd_snapshot
from .clip import _cmd_clip
from .status import _cmd_status
from .arm import _cmd_arm
from .disarm import _cmd_disarm
from .who import _cmd_who
from .help import _cmd_help
from .cameras import _cmd_cameras
from .analyze import _cmd_analyze, _handle_photo
from .events import _cmd_events
from .zones import _cmd_zones
from .time_rules import _cmd_time_rules
from .night import _cmd_night
from .faces import _cmd_faces
from .timelapse import _cmd_timelapse
from .ask import _cmd_ask


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
