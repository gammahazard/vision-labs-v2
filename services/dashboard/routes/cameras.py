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

import asyncio
import json
import shlex

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import cameras as registry

router = APIRouter(prefix="/api/cameras", tags=["cameras"])


async def _ffprobe_rtsp(url: str, timeout: float = 8.0) -> dict:
    """
    Run ffprobe against an RTSP URL and return summary info.
    Tries TCP transport first (more reliable than UDP). Doesn't block the
    event loop — runs the subprocess via asyncio.

    Returns {"ok": True, "codec": ..., "width": ..., "height": ..., "fps": ...}
    on success, or {"ok": False, "error": "..."} on failure.
    """
    if not url or not url.startswith(("rtsp://", "rtsps://")):
        return {"ok": False, "error": "URL must start with rtsp:// or rtsps://"}

    cmd = [
        "ffprobe",
        "-rtsp_transport", "tcp",
        "-v", "error",
        "-show_streams",
        "-show_format",
        "-print_format", "json",
        "-timeout", str(int(timeout * 1_000_000)),  # microseconds
        url,
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout + 2)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"ok": False, "error": f"timeout after {timeout}s"}

        if proc.returncode != 0:
            err_msg = (stderr.decode("utf-8", errors="replace") or "ffprobe failed").strip().splitlines()[-1][:200]
            return {"ok": False, "error": err_msg}

        info = json.loads(stdout)
        # Find the first video stream
        for s in info.get("streams", []):
            if s.get("codec_type") == "video":
                fps_str = s.get("r_frame_rate", "0/1")
                try:
                    num, den = fps_str.split("/")
                    fps = round(float(num) / float(den), 1) if float(den) > 0 else 0
                except Exception:
                    fps = 0
                return {
                    "ok": True,
                    "codec": s.get("codec_name", "?"),
                    "width": s.get("width", 0),
                    "height": s.get("height", 0),
                    "fps": fps,
                }
        return {"ok": False, "error": "No video stream found"}
    except FileNotFoundError:
        return {"ok": False, "error": "ffprobe not installed in dashboard container"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@router.post("/test-rtsp")
async def test_rtsp(request: Request):
    """Probe an RTSP URL to verify it's reachable + decodable before registering."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)
    url = body.get("url", "").strip()
    result = await _ffprobe_rtsp(url)
    # Always return 200 — the {ok: bool} field tells the UI what happened
    return result


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


@router.get("/next-slot")
async def next_slot():
    """Return the next available pre-defined camera slot id, or null if full."""
    return {"slot": registry.next_available_slot()}


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

    # If this camera id matches a pre-defined slot, include the activation
    # command in the response so the UI can show the user how to start it.
    cid = body["id"]
    activation_cmd = None
    if cid in registry.AVAILABLE_SLOTS:
        activation_cmd = f"docker compose --profile {cid} up -d"

    return {
        "ok": True,
        "camera": registry.get_camera(cid),
        "activation_cmd": activation_cmd,
    }


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
    """Remove a camera from the registry.

    Phase 7b: a delete also nudges the orchestrator (via Redis pub/sub
    inside registry.delete_camera), which will tear down the matching
    profile's services within seconds. The dashboard does not invoke
    Docker directly.
    """
    removed = registry.delete_camera(camera_id)
    if not removed:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return {"ok": True}


@router.get("/{camera_id}/status")
async def camera_status(camera_id: str):
    """Latest orchestrator action for this camera's profile.

    Returns the most recent up/down attempt the orchestrator made for the
    profile matching this camera_id, if any. Used by the UI to show a live
    status badge (Pending / Running / Error) after Save without polling
    Docker directly.

    Response shape:
        { "in_registry": bool,
          "enabled": bool | null,
          "slot": str | null,     # only set if the camera id maps to a slot
          "latest_action": { action, success, detail, timestamp } | null }
    """
    entry = registry.get_camera(camera_id)
    in_registry = entry is not None
    enabled = entry.get("enabled", True) if entry else None
    slot = camera_id if camera_id in registry.AVAILABLE_SLOTS else None
    latest = registry.latest_orchestrator_action(slot) if slot else None
    return {
        "in_registry": in_registry,
        "enabled": enabled,
        "slot": slot,
        "latest_action": latest,
    }


@router.patch("/{camera_id}/enabled")
async def set_enabled(camera_id: str, request: Request):
    """Flip the `enabled` flag without otherwise editing the entry.

    Body: { "enabled": true | false }

    The orchestrator picks up the change via the pub/sub event published
    inside registry.upsert_camera and starts/stops the slot's services.
    """
    entry = registry.get_camera(camera_id)
    if not entry:
        return JSONResponse({"error": "Not found"}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    enabled = bool(body.get("enabled"))
    entry["enabled"] = enabled
    ok, err = registry.upsert_camera(entry)
    if not ok:
        return JSONResponse({"error": err}, status_code=400)
    return {"ok": True, "enabled": enabled}
