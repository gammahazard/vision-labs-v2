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


# Per-key validation: type + range. Reject anything out of range BEFORE
# writing to Redis so a misclick in the UI doesn't crash a detector with
# `float("abc")` or `iou_threshold=999`. Keys not in this dict were
# already filtered out of the allowlist above.
_CONFIG_VALIDATORS = {
    "confidence_thresh": ("float", 0.0, 1.0),
    "iou_threshold": ("float", 0.0, 1.0),
    "lost_timeout": ("float", 0.5, 600.0),
    "target_fps": ("float", 1.0, 60.0),
    "notify_person": ("bool", None, None),
    "notify_vehicle": ("bool", None, None),
    "suppress_known": ("bool", None, None),
    "notify_cooldown": ("float", 0.0, 86400.0),
    "vehicle_cooldown": ("float", 0.0, 86400.0),
    "min_keypoints": ("int", 0, 17),
    "kp_confidence_thresh": ("float", 0.0, 1.0),
    "vehicle_confidence_thresh": ("float", 0.0, 1.0),
    "vehicle_idle_timeout": ("float", 1.0, 86400.0),
}


def _validate_config_value(key: str, raw) -> tuple[bool, str]:
    """Validate + coerce a single config value.

    Returns `(ok, stringified_value_or_error_message)`. Strings (the
    Redis-native storage type) are produced for the success path so the
    caller can HSET them directly.
    """
    rule = _CONFIG_VALIDATORS.get(key)
    if rule is None:
        return False, f"unknown key: {key}"
    kind, lo, hi = rule
    try:
        if kind == "bool":
            # Accept 0/1, "0"/"1", "true"/"false", bool.
            s = str(raw).strip().lower()
            if s in ("1", "true", "yes", "on"):
                return True, "1"
            if s in ("0", "false", "no", "off"):
                return True, "0"
            return False, f"{key}: expected boolean"
        if kind == "int":
            v = int(float(raw))
            if v < lo or v > hi:
                return False, f"{key}: must be {lo}-{hi}"
            return True, str(v)
        if kind == "float":
            v = float(raw)
            if v < lo or v > hi:
                return False, f"{key}: must be {lo}-{hi}"
            return True, str(v)
    except (TypeError, ValueError):
        return False, f"{key}: invalid value {raw!r}"
    return False, f"{key}: validator error"


@router.post("/config")
async def update_config(config: dict, camera: str = ""):
    """Update per-camera detector/tracker config. Detectors poll the hash and
    apply changes without a restart. Values are type-checked + range-checked
    before being written so a bad input can't crash a detector."""
    try:
        config_key, _, _, _, _ = _resolve_keys(camera)
        filtered: dict[str, str] = {}
        errors: list[str] = []
        for k, v in config.items():
            if k not in _CONFIG_VALIDATORS:
                continue  # silently drop unknown keys (existing behavior)
            ok, result = _validate_config_value(k, v)
            if ok:
                filtered[k] = result
            else:
                errors.append(result)

        if errors:
            return JSONResponse(
                status_code=400,
                content={"error": "validation failed", "details": errors},
            )

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
