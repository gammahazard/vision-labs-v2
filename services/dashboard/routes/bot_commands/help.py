"""
routes/bot_commands/help.py — Telegram command handler(s).

Extracted from the legacy monolithic bot_commands.py (Phase J modularization).
The function and any per-command helpers live together so adding/changing a
command is a single-file change. ``__init__.py`` wires this into the dispatcher.
"""




from ._shared import (
    send_text, _telegram_get_cameras,
)


async def _cmd_help(chat_id: str = "", **kwargs):
    """Send list of available commands."""
    cams = _telegram_get_cameras()
    if cams:
        cam_examples = " · ".join(
            f"<code>{c.get('name') or c.get('id')}</code>" for c in cams[:4]
        )
        multi_note = (
            "\n📷 <b>Multi-camera</b>\n"
            f"Most commands accept an optional camera name at the end:\n"
            f"  {cam_examples}\n"
            "Use <code>all</code> to fan-out across every camera.\n"
            "Examples: <code>/snapshot basement</code> · <code>/clip 10 front</code> · <code>/events all</code>\n"
            "See <code>/cameras</code> to list available.\n"
        )
    else:
        multi_note = ""

    await send_text(
        "🤖 <b>Vision Labs Bot</b>\n\n"
        "/snapshot [camera] — 📸 Live photo + AI analysis\n"
        "/clip [5-40] [camera] — 🎬 Video clip + AI analysis\n"
        "/analyze [camera] [prompt] — 👁️ AI vision analysis\n"
        "/status [camera] — 📊 System health\n"
        "/who [camera] — 👁️ Who's in frame now\n"
        "/events [1-20] [camera] — 📋 Recent detections\n"
        "/zones [camera] — 🗺️ Camera view with zones drawn\n"
        "/cameras — 📷 List configured cameras\n"
        "/rules — 📜 Time rules overview\n"
        "/night — 🌙 Night mode status\n"
        "/faces — 👤 Enrolled faces\n"
        "/timelapse [YYYY-MM-DD] [camera] — ⏩ Timelapse from snapshots\n"
        "/ask [question] — 🧠 Ask the AI assistant\n\n"
        + multi_note +
        "📷 <b>Send a photo</b> to get AI vision analysis\n\n"
        "🔒 <b>Admin Only</b>\n"
        "/arm — 🟢 Enable notifications\n"
        "/disarm — 🔴 Disable notifications",
        chat_id=chat_id,
    )
