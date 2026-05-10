"""
routes/zones.py — Zone CRUD endpoints.

PURPOSE:
    Manage detection zones stored in Redis. Zones are polygons
    drawn on the camera feed with associated alert levels.

ENDPOINTS:
    GET    /api/zones          — List all zones
    POST   /api/zones          — Create a new zone
    DELETE /api/zones/{zone_id} — Delete a zone
"""

import json
import uuid

from fastapi import APIRouter
from fastapi.responses import JSONResponse

import routes as ctx

router = APIRouter(prefix="/api", tags=["zones"])


@router.get("/zones")
async def list_zones():
    """List all defined zones."""
    try:
        raw = ctx.r.hgetall(ctx.ZONE_KEY)
        zones = []
        for zone_id, zone_json in raw.items():
            zone = json.loads(zone_json)
            zone["id"] = zone_id
            zones.append(zone)
        return {"zones": zones}
    except Exception as e:
        ctx.logger.warning(f"List zones failed: {e}")
        return {"zones": []}


@router.post("/zones")
async def create_zone(data: dict):
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

    if alert_level not in ("always", "night_only", "log_only", "ignore", "dead_zone"):
        alert_level = "log_only"

    # Generate unique zone ID
    zone_id = f"zone_{uuid.uuid4().hex[:8]}"

    zone_data = {
        "name": name,
        "points": points,
        "alert_level": alert_level,
    }

    ctx.r.hset(ctx.ZONE_KEY, zone_id, json.dumps(zone_data))

    ctx.logger.info(f"Zone created: {zone_id} ({name}, {alert_level})")
    return {"id": zone_id, **zone_data}


@router.put("/zones/{zone_id}")
async def update_zone(zone_id: str, data: dict):
    """Update an existing zone's points, name, or alert_level."""
    raw = ctx.r.hget(ctx.ZONE_KEY, zone_id)
    if not raw:
        return JSONResponse(status_code=404, content={"error": "Zone not found"})

    zone = json.loads(raw)

    if "name" in data:
        zone["name"] = data["name"].strip()
    if "points" in data:
        if len(data["points"]) < 3:
            return JSONResponse(status_code=400, content={"error": "Need at least 3 points"})
        zone["points"] = data["points"]
    if "alert_level" in data:
        zone["alert_level"] = data["alert_level"]

    ctx.r.hset(ctx.ZONE_KEY, zone_id, json.dumps(zone))
    ctx.logger.info(f"Zone updated: {zone_id}")
    return {"id": zone_id, **zone}


@router.delete("/zones/{zone_id}")
async def delete_zone(zone_id: str):
    """Delete a zone by ID."""
    deleted = ctx.r.hdel(ctx.ZONE_KEY, zone_id)

    if deleted:
        ctx.logger.info(f"Zone deleted: {zone_id}")
        return {"deleted": True}
    return JSONResponse(status_code=404, content={"error": "Zone not found"})
