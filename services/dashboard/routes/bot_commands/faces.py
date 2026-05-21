"""
routes/bot_commands/faces.py — Telegram command handler(s).

Extracted from the legacy monolithic bot_commands.py (Phase J modularization).
The function and any per-command helpers live together so adding/changing a
command is a single-file change. ``__init__.py`` wires this into the dispatcher.
"""


import httpx

import routes as ctx

from ._shared import (
    send_text,
)


async def _cmd_faces(chat_id: str = "", **kwargs):
    """List enrolled/known faces."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{ctx.FACE_API_URL}/api/faces")
            if resp.status_code != 200:
                await send_text("⚠️ Face recognizer service unavailable", chat_id=chat_id)
                return
            data = resp.json()

        faces = data.get("faces", [])
        if not faces:
            await send_text("👤 No faces enrolled yet — use the dashboard to add people.", chat_id=chat_id)
            return

        # Group by name — each photo angle is a separate DB row
        from collections import Counter
        name_counts = Counter(f.get("name", "unnamed") for f in faces)

        parts = [f"👤 <b>Enrolled Faces</b> ({len(name_counts)} people)\n"]
        for name, count in name_counts.most_common():
            parts.append(f"  • {name} ({count} photo(s))")

        await send_text("\n".join(parts), chat_id=chat_id)
    except Exception as e:
        await send_text(f"⚠️ Failed to fetch faces: {e}", chat_id=chat_id)
