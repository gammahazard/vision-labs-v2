"""
services/dashboard/pollers/ — long-running background asyncio tasks.

PURPOSE:
    Each module here is one `async def …()` that runs in a `while True`
    loop for the lifetime of the dashboard process. They're scheduled
    via `asyncio.create_task(...)` from `server.py`'s `startup()` handler.

WHY HERE AND NOT IN server.py:
    Before extraction, server.py was 1300+ lines because all 5 pollers
    + WebSocket loop + auth + startup wiring were tangled in one file.
    Splitting them out keeps each concern in a file you can read end-to-end.

CURRENT POLLERS:
    - reminders.py        — check due reminders every 60s, send via Telegram
    - ollama_warmup.py    — pull + warm the chat model at startup
    - comfyui_cleanup.py  — clear stale GPU locks at startup
    - retention.py        — daily prune of /data/snapshots + /data/events
    - events.py           — poll events stream, save snapshots, broadcast to Telegram
"""
