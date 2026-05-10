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
