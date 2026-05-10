"""
services/dashboard/cameras.py — camera registry backed by Redis.

PURPOSE:
    Single source of truth for "which cameras exist in this deployment".
    Stored as a Redis hash `cameras:registry` where the field is the
    camera_id and the value is JSON metadata.

DATA SHAPE per entry:
    {
        "id": "front_door",              # primary key, matches CAMERA_ID env
        "name": "Front Door",             # human-readable
        "rtsp_sub": "rtsp://.../sub",     # SD stream used for detection
        "rtsp_main": "rtsp://.../main",   # HD stream for viewing (optional)
        "location_lat": 42.0974,          # optional
        "location_lon": -82.4540,         # optional
        "gpu_id": 0,                       # which GPU detectors should run on
        "enabled": true,                   # if false, services skip it
        # Per-camera detector selection — saves GPU time when a camera doesn't
        # need a given detector. Defaults match the original full-stack behaviour.
        "detect_persons": true,            # pose-detector + tracker
        "detect_vehicles": true,           # vehicle-detector (drop for indoor cams)
        "detect_faces": true,              # face-recognizer
        "created_at": "2026-05-10T22:00:00Z"
    }

NOTE on secrets:
    rtsp_sub/rtsp_main URLs typically include user:pass — these are stored
    in plaintext in Redis (same as today's docker-compose env vars; no
    regression). For a future hardening pass, consider splitting credentials
    into a separate secrets vault.

CURRENT USE:
    Phase 7 makes this an additive scaffold — the existing services still
    read CAMERA_ID env at boot. The registry is there so:
      1. UI can enumerate cameras (camera switcher)
      2. Future compose generators can read from one place
      3. Per-camera config (zones, thresholds) can hang off the entry
"""

import json
import logging
import time
from typing import Optional

import routes as ctx

logger = logging.getLogger("dashboard.cameras")

REGISTRY_KEY = "cameras:registry"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _validate_camera(entry: dict) -> Optional[str]:
    """Return an error message if invalid, None if OK."""
    if not isinstance(entry, dict):
        return "entry must be a JSON object"
    if not entry.get("id") or not isinstance(entry["id"], str):
        return "'id' is required and must be a string"
    # Allow only safe characters in id (used as Redis key suffix + filename)
    cid = entry["id"]
    if not all(c.isalnum() or c in "_-" for c in cid):
        return "'id' must be alphanumeric, dash, or underscore"
    if not entry.get("rtsp_sub"):
        return "'rtsp_sub' is required (sub-stream URL used for detection)"
    return None


def list_cameras() -> list:
    """Return all registered cameras, sorted by id."""
    try:
        raw = ctx.r.hgetall(REGISTRY_KEY)
        if not raw:
            return []
        out = []
        for cid, val in raw.items():
            try:
                out.append(json.loads(val))
            except Exception:
                continue
        out.sort(key=lambda c: c.get("id", ""))
        return out
    except Exception as e:
        logger.warning(f"Registry list failed: {e}")
        return []


def get_camera(camera_id: str) -> Optional[dict]:
    """Return a single camera entry, or None if not found."""
    try:
        raw = ctx.r.hget(REGISTRY_KEY, camera_id)
        if raw:
            return json.loads(raw)
    except Exception as e:
        logger.warning(f"Registry get({camera_id}) failed: {e}")
    return None


def upsert_camera(entry: dict) -> tuple[bool, Optional[str]]:
    """
    Insert or update a camera. Returns (ok, error_message).
    Sets created_at if absent; updates updated_at every time.
    """
    err = _validate_camera(entry)
    if err:
        return False, err
    cid = entry["id"]
    try:
        existing_raw = ctx.r.hget(REGISTRY_KEY, cid)
        existing = json.loads(existing_raw) if existing_raw else None
        if existing and "created_at" in existing:
            entry["created_at"] = existing["created_at"]
        else:
            entry.setdefault("created_at", _now_iso())
        entry["updated_at"] = _now_iso()
        entry.setdefault("enabled", True)
        entry.setdefault("gpu_id", 0)
        # Detector selection defaults — keep the all-on behaviour the system
        # had before this field existed, so single-camera deployments don't
        # silently lose detection types.
        entry.setdefault("detect_persons", True)
        entry.setdefault("detect_vehicles", True)
        entry.setdefault("detect_faces", True)
        ctx.r.hset(REGISTRY_KEY, cid, json.dumps(entry))
        logger.info(f"Registry upsert: {cid} ({entry.get('name', cid)})")
        return True, None
    except Exception as e:
        return False, str(e)


def delete_camera(camera_id: str) -> bool:
    """Remove a camera from the registry. Returns True if it existed."""
    try:
        return bool(ctx.r.hdel(REGISTRY_KEY, camera_id))
    except Exception as e:
        logger.warning(f"Registry delete({camera_id}) failed: {e}")
        return False


def seed_default_if_empty(default_id: str, default_name: str,
                          rtsp_sub: str, rtsp_main: str = "",
                          location_lat: float = 0.0,
                          location_lon: float = 0.0) -> bool:
    """
    On first startup, register the single camera that the env-var-based
    config describes. After that, the registry takes over and seeding
    is a no-op (returns False).

    If rtsp_sub is empty (e.g. dashboard env doesn't have it), we still
    seed a stub entry so the UI has something to list — user can fill in
    the URL via PUT /api/cameras/{id} later.

    Returns True if a seed was written.
    """
    try:
        if ctx.r.exists(REGISTRY_KEY):
            return False

        # Build the entry directly so we can include a placeholder rtsp_sub
        # when the env vars aren't passed through (single-cam setups often
        # only configure ingester/recorder envs, not dashboard).
        entry = {
            "id": default_id,
            "name": default_name,
            "rtsp_sub": rtsp_sub or "rtsp://configure-me/",
            "rtsp_main": rtsp_main,
            "location_lat": location_lat,
            "location_lon": location_lon,
            "gpu_id": 0,
            "enabled": True,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        err = _validate_camera(entry)
        if err:
            logger.warning(f"Default-camera seed validation failed: {err}")
            return False
        ctx.r.hset(REGISTRY_KEY, default_id, json.dumps(entry))
        status = "with RTSP URL" if rtsp_sub else "(rtsp_sub blank — set it via PUT /api/cameras/{id})"
        logger.info(f"Camera registry seeded with default '{default_id}' {status}")
        return True
    except Exception as e:
        logger.warning(f"Registry seed failed: {e}")
        return False
