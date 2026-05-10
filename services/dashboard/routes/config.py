"""
routes/config.py — Config and stats endpoints.

PURPOSE:
    GET  /api/config — Read current detector/tracker settings from Redis.
    POST /api/config — Update detector/tracker settings in Redis.
    GET  /api/stats  — Return system stats (stream lengths, state).
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse
import redis

import routes as ctx

router = APIRouter(prefix="/api", tags=["config"])


@router.get("/config")
async def get_config():
    """
    Return the current detector/tracker config from Redis.
    The dashboard reads this to populate the settings sliders.
    """
    try:
        config = ctx.r.hgetall(ctx.CONFIG_KEY)
        if not config:
            config = ctx.DEFAULT_CONFIG.copy()
        return {"config": config}
    except redis.ConnectionError:
        return JSONResponse(status_code=503, content={"error": "Redis unavailable"})


@router.post("/config")
async def update_config(config: dict):
    """
    Update detector/tracker config in Redis.

    The pose-detector and tracker services poll this key periodically
    and apply new settings without requiring a restart.
    """
    try:
        # Only allow known config keys
        allowed_keys = {
            "confidence_thresh", "iou_threshold", "lost_timeout", "target_fps",
            "notify_person", "notify_vehicle", "suppress_known",
            "notify_cooldown", "vehicle_cooldown",
            "min_keypoints", "kp_confidence_thresh",
            "vehicle_confidence_thresh", "vehicle_idle_timeout",
        }
        filtered = {k: str(v) for k, v in config.items() if k in allowed_keys}

        if filtered:
            ctx.r.hset(ctx.CONFIG_KEY, mapping=filtered)
            ctx.logger.info(f"Config updated: {filtered}")

        return {"config": ctx.r.hgetall(ctx.CONFIG_KEY)}
    except redis.ConnectionError:
        return JSONResponse(status_code=503, content={"error": "Redis unavailable"})


@router.get("/stats")
async def get_stats():
    """
    Return system stats: stream lengths, current state, uptime info.
    """
    try:
        stats = {
            "frames_in_stream": ctx.r.xlen(ctx.FRAME_STREAM),
            "detections_in_stream": ctx.r.xlen(ctx.DETECTION_STREAM),
            "events_in_stream": ctx.r.xlen(ctx.EVENT_STREAM),
            "state": ctx.r.hgetall(ctx.STATE_KEY),
            "config": ctx.r.hgetall(ctx.CONFIG_KEY),
        }
        return stats
    except redis.ConnectionError:
        return JSONResponse(status_code=503, content={"error": "Redis unavailable"})
