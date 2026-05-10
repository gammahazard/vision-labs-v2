"""
routes/unknowns.py — Unknown face proxy endpoints.

PURPOSE:
    Proxy all unknown face management requests to the face-recognizer
    service. Supports listing, viewing photos, labeling (promoting
    to known), clearing all, and deleting individual unknowns.

    When an unknown is labeled, emits a person_identified event to
    the event stream so it appears in the event feed.

ENDPOINTS:
    GET    /api/unknowns              — List auto-captured unknowns
    GET    /api/unknowns/{uid}/photo  — Get unknown face thumbnail
    POST   /api/unknowns/{uid}/label  — Label (promote to known face)
    DELETE /api/unknowns/clear        — Clear all unknowns
    DELETE /api/unknowns/{uid}        — Delete single unknown
"""

import json
import time

import redis
from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response
import httpx

import routes as ctx

router = APIRouter(prefix="/api", tags=["unknowns"])


@router.get("/unknowns")
async def list_unknowns():
    """Proxy: list auto-captured unknown faces."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{ctx.FACE_API_URL}/api/unknowns", timeout=5)
            return resp.json()
    except Exception as e:
        ctx.logger.warning(f"Unknown faces API unavailable: {e}")
        return JSONResponse(status_code=503, content={"error": "Face recognizer not available"})


@router.get("/unknowns/{uid}/photo")
async def get_unknown_photo(uid: int):
    """Proxy: get unknown face thumbnail."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{ctx.FACE_API_URL}/api/unknowns/{uid}/photo", timeout=5)
            if resp.status_code == 200:
                return Response(content=resp.content, media_type="image/jpeg")
            return JSONResponse(status_code=404, content={"error": "Unknown face not found"})
    except Exception as e:
        return JSONResponse(status_code=503, content={"error": "Face recognizer not available"})


@router.post("/unknowns/{uid}/label")
async def label_unknown(uid: int, data: dict):
    """Proxy: promote unknown face to known by assigning a name."""
    try:
        async with httpx.AsyncClient() as client:
            # Fetch the face crop photo BEFORE labeling — labeling deletes the
            # unknown from SQLite, so the photo would be gone after the POST.
            face_photo = None
            try:
                photo_resp = await client.get(
                    f"{ctx.FACE_API_URL}/api/unknowns/{uid}/photo",
                    timeout=5,
                )
                if photo_resp.status_code == 200 and photo_resp.content:
                    face_photo = photo_resp.content
            except Exception as e:
                ctx.logger.debug(f"Failed to pre-fetch face crop for uid {uid}: {e}")

            # Now label the unknown (this deletes it from unknown_faces
            # and moves it to known_faces)
            resp = await client.post(
                f"{ctx.FACE_API_URL}/api/unknowns/{uid}/label",
                json=data,
                timeout=10,
            )
            result = resp.json()

            # On success, emit a person_identified event to the event feed
            if resp.status_code == 200 and result.get("success"):
                name = data.get("name", "Unknown")

                # If we didn't get the photo before, try the newly-created
                # known face (label returns the new face_id)
                if not face_photo:
                    new_fid = result.get("face_id")
                    if new_fid:
                        try:
                            photo_resp = await client.get(
                                f"{ctx.FACE_API_URL}/api/faces/{new_fid}/photo",
                                timeout=5,
                            )
                            if photo_resp.status_code == 200 and photo_resp.content:
                                face_photo = photo_resp.content
                        except Exception:
                            pass

                # Store the face crop in Redis so the event poller
                # sends it as the Telegram notification photo
                snapshot_key = ""
                if face_photo:
                    try:
                        ts = int(time.time())
                        snapshot_key = f"person_snapshot:{ctx.CAMERA_ID}:{ts}"
                        ctx.r_bin.setex(snapshot_key, 7200, face_photo)  # 2h TTL
                        ctx.logger.info(f"Saved face crop snapshot: {snapshot_key}")
                    except Exception as e:
                        ctx.logger.warning(f"Failed to save face crop to Redis: {e}")
                        snapshot_key = ""

                try:
                    event = {
                        "event_type": "person_identified",
                        "person_id": f"unknown_{uid}",
                        "identity_name": name,
                        "camera_id": ctx.CAMERA_ID,
                        "action": "labeled",
                        "timestamp": str(time.time()),
                    }
                    if snapshot_key:
                        event["snapshot_key"] = snapshot_key
                    ctx.r.xadd(ctx.EVENT_STREAM, event)
                    ctx.logger.info(f"Unknown {uid} labeled as '{name}' — event emitted (snapshot_key={snapshot_key or 'none'})")
                except Exception as e:
                    ctx.logger.warning(f"Failed to emit label event: {e}")

            return JSONResponse(status_code=resp.status_code, content=result)
    except Exception as e:
        ctx.logger.warning(f"Label unknown failed: {e}")
        return JSONResponse(status_code=503, content={"error": "Face recognizer not available"})


@router.delete("/unknowns/clear")
async def clear_all_unknowns():
    """Proxy: remove all auto-captured unknown faces."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.delete(f"{ctx.FACE_API_URL}/api/unknowns", timeout=5)
            return resp.json()
    except Exception as e:
        ctx.logger.warning(f"Clear all unknowns failed: {e}")
        return JSONResponse(status_code=503, content={"error": "Face recognizer not available"})


@router.delete("/unknowns/{uid}")
async def delete_unknown(uid: int):
    """Proxy: remove an auto-captured unknown face."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.delete(f"{ctx.FACE_API_URL}/api/unknowns/{uid}", timeout=5)
            return resp.json()
    except Exception as e:
        ctx.logger.warning(f"Delete unknown failed: {e}")
        return JSONResponse(status_code=503, content={"error": "Face recognizer not available"})
