"""
routes/config.py — Config and stats endpoints.

PURPOSE:
    GET  /api/config?camera=<id> — Read detector/tracker settings for a camera.
    POST /api/config?camera=<id> — Update detector/tracker settings for a camera.
    GET  /api/stats?camera=<id>  — Return system stats for a camera.

If `camera` is omitted, falls back to the dashboard's primary camera
(ctx.CAMERA_ID env). Each camera has independent config:{camera_id} and
state:{camera_id} hashes in Redis.
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse
import redis

import routes as ctx

router = APIRouter(prefix="/api", tags=["config"])


def _resolve_keys(camera: str) -> tuple[str, str, str, str, str]:
    """
    Return (config_key, state_key, frame_stream, det_stream, evt_stream) for the
    requested camera. Falls back to the dashboard's primary keys if no camera
    is specified, so legacy callers without ?camera still work.
    """
    if not camera or camera == ctx.CAMERA_ID:
        return (ctx.CONFIG_KEY, ctx.STATE_KEY,
                ctx.FRAME_STREAM, ctx.DETECTION_STREAM, ctx.EVENT_STREAM)
    from contracts.streams import (
        CONFIG_KEY as _CFG_TMPL, STATE_KEY as _STATE_TMPL,
        FRAME_STREAM as _FRAME_TMPL, DETECTION_STREAM as _DET_TMPL,
        EVENT_STREAM as _EVT_TMPL, stream_key as _stream_key,
    )
    return (
        _stream_key(_CFG_TMPL, camera_id=camera),
        _stream_key(_STATE_TMPL, camera_id=camera),
        _stream_key(_FRAME_TMPL, camera_id=camera),
        _stream_key(_DET_TMPL, detector_type="pose", camera_id=camera),
        _stream_key(_EVT_TMPL, camera_id=camera),
    )


@router.get("/config")
async def get_config(camera: str = ""):
    """Return per-camera detector/tracker config. Pass ?camera=<id> to scope."""
    try:
        config_key, _, _, _, _ = _resolve_keys(camera)
        config = ctx.r.hgetall(config_key)
        if not config:
            config = ctx.DEFAULT_CONFIG.copy()
        return {"config": config, "camera": camera or ctx.CAMERA_ID}
    except redis.ConnectionError:
        return JSONResponse(status_code=503, content={"error": "Redis unavailable"})


@router.post("/config")
async def update_config(config: dict, camera: str = ""):
    """Update per-camera detector/tracker config. Detectors poll the hash and
    apply changes without a restart."""
    try:
        config_key, _, _, _, _ = _resolve_keys(camera)
        allowed_keys = {
            "confidence_thresh", "iou_threshold", "lost_timeout", "target_fps",
            "notify_person", "notify_vehicle", "suppress_known",
            "notify_cooldown", "vehicle_cooldown",
            "min_keypoints", "kp_confidence_thresh",
            "vehicle_confidence_thresh", "vehicle_idle_timeout",
        }
        filtered = {k: str(v) for k, v in config.items() if k in allowed_keys}

        if filtered:
            ctx.r.hset(config_key, mapping=filtered)
            ctx.logger.info(f"Config updated for {config_key}: {filtered}")

        return {"config": ctx.r.hgetall(config_key), "camera": camera or ctx.CAMERA_ID}
    except redis.ConnectionError:
        return JSONResponse(status_code=503, content={"error": "Redis unavailable"})


@router.get("/stats")
async def get_stats(camera: str = ""):
    """Per-camera system stats (stream lengths, state, config)."""
    try:
        cfg_key, state_key, frame_stream, det_stream, evt_stream = _resolve_keys(camera)
        stats = {
            "camera": camera or ctx.CAMERA_ID,
            "frames_in_stream": ctx.r.xlen(frame_stream),
            "detections_in_stream": ctx.r.xlen(det_stream),
            "events_in_stream": ctx.r.xlen(evt_stream),
            "state": ctx.r.hgetall(state_key),
            "config": ctx.r.hgetall(cfg_key),
        }
        return stats
    except redis.ConnectionError:
        return JSONResponse(status_code=503, content={"error": "Redis unavailable"})
