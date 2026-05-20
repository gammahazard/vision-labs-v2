"""
routes/bot_commands/ — split package (was bot_commands.py monolith).

Backward-compat surface (only thing server.py imports):
    from routes.bot_commands import poll_telegram_callbacks

Internal layout:
    _shared.py    — constants, logging, audit helpers, camera resolution
    _dispatch.py  — _handle_command router (cmd → handler)
    _poller.py    — long-poll loop (poll_telegram_callbacks)
    <command>.py  — one file per command: snapshot, clip, status, arm, disarm,
                    who, help, cameras, analyze, events, zones, time_rules,
                    night, faces, timelapse, ask
"""

from ._poller import poll_telegram_callbacks

__all__ = ["poll_telegram_callbacks"]
