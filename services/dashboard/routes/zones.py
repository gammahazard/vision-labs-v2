"""
routes/zones.py — Zone CRUD endpoints (per-camera).

PURPOSE:
    Manage detection zones stored in Redis. Zones are per-camera polygons
    (zones:{camera_id} hash). Pass `?camera=<id>` on every endpoint to scope
    operations to that camera; omit to default to the dashboard's primary.
"""

import json
import uuid

from fastapi import APIRouter
from fastapi.responses import JSONResponse

import routes as ctx

router = APIRouter(prefix="/api", tags=["zones"])

# Single source of truth for valid zone alert levels. Shared between
# POST and PUT so they can't disagree on what's accepted.
#   always      — fire alerts whenever a detection lands in this zone
#   night_only  — only at night / late-night per contracts.time_rules
#   log_only    — record events but suppress notifications
#   ignore      — skip alerting but keep the detection on screen
#   dead_zone   — drop the detection entirely (no event, no overlay)
_VALID_ALERT_LEVELS = frozenset({
    "always", "night_only", "log_only", "ignore", "dead_zone",
})


def _zone_key(camera: str) -> str:
    """Return the Redis zone-hash key for `camera` (defaults to primary)."""
    if not camera or camera == ctx.CAMERA_ID:
        return ctx.ZONE_KEY
    from contracts.streams import ZONE_KEY as _ZONE_TMPL, stream_key as _stream_key
    return _stream_key(_ZONE_TMPL, camera_id=camera)


@router.get("/zones")
async def list_zones(camera: str = ""):
    """List zones for a camera. Pass ?camera=<id> to scope."""
    try:
        raw = ctx.r.hgetall(_zone_key(camera))
        zones = []
        for zone_id, zone_json in raw.items():
            zone = json.loads(zone_json)
            zone["id"] = zone_id
            zones.append(zone)
        return {"zones": zones, "camera": camera or ctx.CAMERA_ID}
    except Exception as e:
        ctx.logger.warning(f"List zones failed: {e}")
        return {"zones": []}


@router.post("/zones")
async def create_zone(data: dict, camera: str = ""):
    """
    Create a new zone.

    Expected body:
        name: str — display name
        points: list[list[float]] — polygon vertices (normalized 0-1)
        alert_level: str — "always", "night_only", "log_only", "ignore"
    """
    name = data.get("name", "Zone").strip()
    points = data.get("points", [])
    alert_level = data.get("alert_level", "log_only")

    if len(points) < 3:
        return JSONResponse(
            status_code=400,
            content={"error": "Zone must have at least 3 points"},
        )

    if alert_level not in _VALID_ALERT_LEVELS:
        alert_level = "log_only"

    # Generate unique zone ID
    zone_id = f"zone_{uuid.uuid4().hex[:8]}"

    zone_data = {
        "name": name,
        "points": points,
        "alert_level": alert_level,
    }

    zk = _zone_key(camera)
    ctx.r.hset(zk, zone_id, json.dumps(zone_data))

    ctx.logger.info(f"Zone created on {zk}: {zone_id} ({name}, {alert_level})")
    return {"id": zone_id, **zone_data}


@router.put("/zones/{zone_id}")
async def update_zone(zone_id: str, data: dict, camera: str = ""):
    """Update an existing zone's points, name, or alert_level."""
    zk = _zone_key(camera)
    raw = ctx.r.hget(zk, zone_id)
    if not raw:
        return JSONResponse(status_code=404, content={"error": "Zone not found"})

    zone = json.loads(raw)

    if "name" in data:
        # `name` may be sent as None/non-string by a buggy client; coerce
        # safely (same handling as POST) so we don't 500 on .strip().
        zone["name"] = (data.get("name") or "Zone").strip() if isinstance(data.get("name"), str) else "Zone"
    if "points" in data:
        if len(data["points"]) < 3:
            return JSONResponse(status_code=400, content={"error": "Need at least 3 points"})
        zone["points"] = data["points"]
    if "alert_level" in data:
        # PUT must validate against the same enum as POST — otherwise a
        # PATCH-ish update can sneak in `alert_level: "destroy"` that the
        # tracker doesn't recognize.
        if data["alert_level"] not in _VALID_ALERT_LEVELS:
            return JSONResponse(
                status_code=400,
                content={"error": f"alert_level must be one of {sorted(_VALID_ALERT_LEVELS)}"},
            )
        zone["alert_level"] = data["alert_level"]

    ctx.r.hset(zk, zone_id, json.dumps(zone))
    ctx.logger.info(f"Zone updated on {zk}: {zone_id}")
    return {"id": zone_id, **zone}


@router.delete("/zones/{zone_id}")
async def delete_zone(zone_id: str, camera: str = ""):
    """Delete a zone by ID."""
    zk = _zone_key(camera)
    deleted = ctx.r.hdel(zk, zone_id)

    if deleted:
        ctx.logger.info(f"Zone deleted from {zk}: {zone_id}")
        return {"deleted": True}
    return JSONResponse(status_code=404, content={"error": "Zone not found"})
