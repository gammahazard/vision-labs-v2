"""
routes/cameras.py — REST API for the camera registry.

PURPOSE:
    Read + admin endpoints for the cameras:registry Redis hash.
    Backed by services/dashboard/cameras.py (the registry module).

ENDPOINTS:
    GET  /api/cameras           — list all cameras
    GET  /api/cameras/{id}      — fetch one
    POST /api/cameras           — register or update one (admin)
    PUT  /api/cameras/{id}      — update one (admin)
    DELETE /api/cameras/{id}    — remove one (admin)

WHY THIS EXISTS (Phase 7 of REFACTOR_PLAN.md):
    Scaffold multi-camera support. The actual per-camera service spawning
    (Phase 7b) reads from this registry. Until 7b, the registry is read-
    only informational for the UI: today's single camera is seeded from
    env vars and still served via the existing single-CAMERA_ID services.

AUTH:
    All endpoints require a valid session cookie (enforced by the HTTP
    middleware in server.py). Mutating endpoints additionally require the
    user to be the `admin` role (current single-user system means only
    the admin account exists).
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import cameras as registry

router = APIRouter(prefix="/api/cameras", tags=["cameras"])


@router.get("")
async def list_all():
    """List every registered camera."""
    return {"cameras": registry.list_cameras()}


@router.get("/{camera_id}")
async def get_one(camera_id: str):
    """Fetch a single camera by id."""
    entry = registry.get_camera(camera_id)
    if not entry:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return entry


@router.post("")
async def create_or_update(request: Request):
    """Register a new camera, or replace an existing one. Idempotent on `id`."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    ok, err = registry.upsert_camera(body)
    if not ok:
        return JSONResponse({"error": err}, status_code=400)
    return {"ok": True, "camera": registry.get_camera(body["id"])}


@router.put("/{camera_id}")
async def update_one(camera_id: str, request: Request):
    """Update an existing camera (must already be registered)."""
    existing = registry.get_camera(camera_id)
    if not existing:
        return JSONResponse({"error": "Not found"}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    body["id"] = camera_id  # path id wins; ignore any body override
    ok, err = registry.upsert_camera(body)
    if not ok:
        return JSONResponse({"error": err}, status_code=400)
    return {"ok": True, "camera": registry.get_camera(camera_id)}


@router.delete("/{camera_id}")
async def delete_one(camera_id: str):
    """Remove a camera from the registry. Doesn't tear down any services."""
    removed = registry.delete_camera(camera_id)
    if not removed:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return {"ok": True}
