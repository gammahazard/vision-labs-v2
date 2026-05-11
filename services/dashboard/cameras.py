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

# Phase 7c: pre-defined camera slots. Each slot has a profile-gated set of
# services in docker-compose.yml. When a user registers a camera with one of
# these IDs, running `docker compose --profile <slot> up -d` starts its detectors.
# To add more slots: duplicate the cam2 block in docker-compose.yml and append here.
AVAILABLE_SLOTS = ["cam2"]


def next_available_slot() -> Optional[str]:
    """Return the next slot id not currently used by a registered camera, or None."""
    used = set()
    try:
        raw = ctx.r.hgetall(REGISTRY_KEY)
        for val in raw.values():
            try:
                used.add(json.loads(val).get("id", ""))
            except Exception:
                continue
    except Exception:
        pass
    for slot in AVAILABLE_SLOTS:
        if slot not in used:
            return slot
    return None


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


# ---------------------------------------------------------------------------
# Shared camera-resolution helpers (used by ai_tools, bot_commands, routes)
# ---------------------------------------------------------------------------
# Why these live here instead of in each caller:
#   - ai_tools._resolve_camera, bot_commands._telegram_get_cameras + helpers,
#     and routes/events._enabled_camera_ids all do the same thing with subtle
#     variations. Single source of truth here means a registry-schema tweak
#     only touches one file.

def list_enabled_cameras() -> list:
    """Return all enabled cameras from the registry, sorted by id.
    Each entry is the full registry dict (id, name, rtsp_sub, detect_*, etc.)."""
    return [c for c in list_cameras() if c.get("enabled", True)]


def enabled_camera_ids() -> list:
    """Just the ids of enabled cameras, sorted. Most callers want this shape."""
    return [c["id"] for c in list_enabled_cameras() if c.get("id")]


def camera_friendly_name(camera_id: str) -> str:
    """Look up a camera's display name, falling back to its id."""
    for c in list_cameras():
        if c.get("id") == camera_id:
            return c.get("name") or camera_id
    return camera_id


def resolve_camera_arg(arg: str, primary_camera_id: str) -> list:
    """
    Resolve a tool/route `camera` argument into a concrete list of camera ids.

    Convention (shared by AI tools, REST routes, etc.):
        ""  / "primary" / absent  → [primary_camera_id]  (or first enabled if missing)
        "all"                      → every enabled camera id
        "<id>"                     → [<id>] if registered+enabled, else []

    Returns an empty list iff `arg` was a specific id that doesn't exist —
    caller should handle this as a user error (unknown camera).
    """
    arg = (arg or "").strip()
    cam_ids = enabled_camera_ids()

    if not arg or arg.lower() == "primary":
        if primary_camera_id in cam_ids:
            return [primary_camera_id]
        return cam_ids[:1] if cam_ids else [primary_camera_id]
    if arg.lower() == "all":
        return cam_ids if cam_ids else [primary_camera_id]
    return [arg] if arg in cam_ids else []


def find_camera_in_tokens(text: str, primary_camera_id: str) -> tuple:
    """
    Scan free-text `text` for a camera identifier; used by Telegram commands
    where the camera lives anywhere in the message ("/clip basement 10s",
    "/clip 10 basement", etc.).

    Match priority for each token:
        1. lowercase == "all"                              → every enabled camera
        2. lowercase == camera id (case-insensitive)        → that camera
        3. lowercase == camera name                         → that camera
        4. unambiguous prefix match (>= 3 chars, single hit) → that camera

    Returns (camera_ids, remaining_text):
        camera_ids: list[str], possibly the primary fallback if no token matched
        remaining_text: original text with the matched camera token stripped
                        (so per-command arg parsing can run on what's left)
    """
    cams = list_enabled_cameras()
    cam_ids = [c["id"] for c in cams]

    id_map = {c["id"].lower(): c["id"] for c in cams}
    name_map = {(c.get("name") or "").lower(): c["id"]
                for c in cams if c.get("name")}

    tokens = (text or "").split()
    new_tokens: list = []
    matched: list | None = None

    for tok in tokens:
        if matched is None:
            tlow = tok.lower()
            if tlow == "all":
                matched = cam_ids if cam_ids else None
                if matched is not None:
                    continue
            if tlow in id_map:
                matched = [id_map[tlow]]
                continue
            if tlow in name_map:
                matched = [name_map[tlow]]
                continue
            if len(tlow) >= 3:
                hits = set()
                for key, cid in id_map.items():
                    if key.startswith(tlow):
                        hits.add(cid)
                for key, cid in name_map.items():
                    if key.startswith(tlow):
                        hits.add(cid)
                if len(hits) == 1:
                    matched = list(hits)
                    continue
        new_tokens.append(tok)

    if matched is None:
        matched = [primary_camera_id] if primary_camera_id else (cam_ids[:1] or [])

    return matched, " ".join(new_tokens)


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
