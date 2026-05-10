"""
services/dashboard/pollers/reminders.py — scheduled Telegram reminders.

PURPOSE:
    Every 60 seconds, query the AI database for reminders whose due_at
    has passed and haven't been sent yet. Send each via Telegram (text,
    snapshot, or video clip — caller chose at creation) and mark sent.

RELATIONSHIPS:
    - Created by: routes/ai_tools.py `schedule_reminder` tool — that tool
      inserts a row into ai.db with a due_at timestamp.
    - Read from: ai.db reminders table (via the AIDB instance passed in).
    - Sends via: routes.notifications.send_text / send_photo / send_video.
    - Scheduled by: server.py startup() once at boot.

WHY 60-SECOND CADENCE:
    Reminders are minute-grained — sub-minute precision is unnecessary
    and the API cost of polling more often (DB read + Telegram check)
    outweighs the marginal latency improvement.
"""

import asyncio
import logging

logger = logging.getLogger("dashboard.reminders")


async def reminder_poller(ai_db):
    """Background task: check for due reminders every 60 seconds and send via Telegram."""
    # Lazy import: notifications.py imports constants which imports os —
    # importing at function level keeps pollers/__init__.py side-effect-free
    # and avoids circular import risk during dashboard module loading.
    from routes.notifications import (
        send_text, send_photo, send_video, is_configured,
        get_latest_frame, build_clip,
    )

    await asyncio.sleep(10)  # Initial delay — let other startup tasks settle
    while True:
        try:
            if is_configured() and ai_db:
                due = ai_db.get_due_reminders()
                for reminder in due:
                    try:
                        msg = reminder["message"]
                        media_type = reminder.get("media_type", "text")

                        if media_type == "snapshot":
                            frame = get_latest_frame()
                            if frame:
                                await send_photo(frame, f"⏰ Reminder: {msg}")
                            else:
                                await send_text(f"⏰ Reminder: {msg}\n\n(Snapshot unavailable — camera may be offline)")
                        elif media_type == "clip":
                            clip = build_clip(duration=5.0, fps=10)
                            if clip:
                                await send_video(clip, f"⏰🎬 Reminder: {msg}")
                            else:
                                await send_text(f"⏰ Reminder: {msg}\n\n(Video clip unavailable — camera may be offline)")
                        else:
                            await send_text(f"⏰ Reminder: {msg}")

                        ai_db.mark_reminder_sent(reminder["id"])
                        logger.info(f"Sent reminder {reminder['id']} ({media_type}): {msg}")
                    except Exception as e:
                        logger.warning(f"Failed to send reminder {reminder['id']}: {e}")
        except Exception as e:
            logger.warning(f"Reminder poller error: {e}")
        await asyncio.sleep(60)
