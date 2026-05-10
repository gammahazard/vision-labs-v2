"""
services/dashboard/pollers/comfyui_cleanup.py — clear stale GPU state at boot.

PURPOSE:
    On dashboard startup, talk to ComfyUI and clean up any stale state left
    over from an unclean shutdown:
      1. Interrupt any running generation
      2. Clear any pending queue items
      3. DELETE the Redis keys `gpu:generation_active` and `gpu:generation_lock`

WHY THIS MATTERS:
    `gpu:generation_lock` has a 360s TTL — if the dashboard crashed mid-
    generation, the next user-triggered generation would be blocked for
    up to 6 minutes waiting for the lock to expire. Clearing it at boot
    eliminates that hang.

    `gpu:generation_active` tells the three detector services + face-recognizer
    to pause their GPU work. If left stale, all 4 services would idle until
    the key was eventually cleared by something else.

RETRY LOGIC:
    ComfyUI takes 30-60 seconds to come online on first boot (loads model
    indexes, scans extensions). We retry 12 times with 5s sleep between =
    60s total wait window before giving up.
"""

import asyncio
import logging

import routes as ctx

logger = logging.getLogger("dashboard.comfyui_cleanup")


async def clear_comfyui_queue():
    """Clear any stale ComfyUI queue items and GPU lock keys from a prior session."""
    import httpx
    from constants import COMFYUI_HOST

    # Wait up to 60s for ComfyUI to come online
    for attempt in range(12):
        try:
            async with httpx.AsyncClient() as client:
                # Interrupt any running job
                await client.post(f"{COMFYUI_HOST}/interrupt", timeout=5)
                # Clear pending queue
                queue_resp = await client.get(f"{COMFYUI_HOST}/queue", timeout=5)
                if queue_resp.status_code == 200:
                    queue_data = queue_resp.json()
                    pending = queue_data.get("queue_pending", [])
                    if pending:
                        pending_ids = [item[1] for item in pending if len(item) > 1]
                        if pending_ids:
                            await client.post(
                                f"{COMFYUI_HOST}/queue",
                                json={"delete": pending_ids},
                                timeout=5,
                            )
                            logger.info(f"Startup: cleared {len(pending_ids)} stale ComfyUI queue items")
                    else:
                        logger.info("Startup: ComfyUI queue is clean")
            # Clear GPU pause flag AND stale generation lock from Redis.
            # Without clearing the lock, an unclean shutdown mid-generation blocks
            # the first new generation for up to 6 min (the lock's SETEX TTL).
            try:
                ctx.r.delete("gpu:generation_active", "gpu:generation_lock")
                logger.info("Startup: cleared GPU pause flag and stale generation lock")
            except Exception:
                pass
            return
        except Exception:
            await asyncio.sleep(5)
    logger.warning("Startup: ComfyUI not reachable after 60s — skipping queue cleanup")
