"""
routes/bot_commands/who.py — Telegram command handler(s).

Extracted from the legacy monolithic bot_commands.py (Phase J modularization).
The function and any per-command helpers live together so adding/changing a
command is a single-file change. ``__init__.py`` wires this into the dispatcher.
"""

import json


import routes as ctx

from ._shared import (
    send_text, _now_str,
    _telegram_get_cameras, _camera_friendly_name, _resolve_camera_token,
)


async def _cmd_who(chat_id: str = "", text: str = "", **kwargs):
    """Report who/what is currently in frame across one or more cameras.

    Default = all cameras (aggregated, since 'who is around right now?' is
    inherently a system-wide question). Specify a camera to narrow:
      /who           — all cameras
      /who basement  — basement only
    """
    from contracts.streams import STATE_KEY as _STATE_TMPL, stream_key as _stream_key
    try:
        cam_ids, _ = _resolve_camera_token(text)
        # Default to "all" for /who when no token was present
        token_present = any(
            tok.lower() in (
                {c["id"].lower() for c in _telegram_get_cameras()} |
                {(c.get("name") or "").lower() for c in _telegram_get_cameras()} |
                {"all"}
            )
            for tok in text.split()
        )
        if not token_present:
            cam_ids = [c["id"] for c in _telegram_get_cameras()] or cam_ids

        parts = ["👁️ <b>Current Scene</b>"]
        for cid in cam_ids:
            friendly = _camera_friendly_name(cid)
            state_key = _stream_key(_STATE_TMPL, camera_id=cid)
            state = ctx.r.hgetall(state_key) or {}

            parts.append(f"\n<b>📷 {friendly}</b>")
            if not state:
                parts.append("  • no detection state — may be clear")
                continue

            num_people = int(state.get("num_people", "0") or 0)
            if num_people > 0:
                parts.append(f"  • People: {num_people}")
                try:
                    people = json.loads(state.get("people", "[]"))
                    for p in people[:5]:
                        name = p.get("identity_name", p.get("id", "unknown"))
                        action = p.get("action", "")
                        parts.append(f"    — {name}{f' ({action})' if action else ''}")
                except json.JSONDecodeError:
                    pass
            else:
                parts.append("  • People: none")

        parts.append(f"\n🕐 {_now_str()}")
        await send_text("\n".join(parts), chat_id=chat_id)
    except Exception as e:
        await send_text(f"⚠️ Failed to read scene state: {e}", chat_id=chat_id)
