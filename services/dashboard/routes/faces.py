"""
routes/faces.py — Face enrollment proxy endpoints.

PURPOSE:
    Proxy all face enrollment requests to the face-recognizer service.
    The dashboard acts as a gateway so the browser only needs
    to talk to one host.

ENDPOINTS:
    GET    /api/faces              — List enrolled faces
    POST   /api/faces/preview      — Preview face crop (no enrollment)
    POST   /api/faces/enroll       — Enroll a new face
    DELETE /api/faces/{face_id}    — Delete an enrolled face
    GET    /api/faces/{face_id}/photo — Get face thumbnail
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response
import httpx

import routes as ctx

router = APIRouter(prefix="/api", tags=["faces"])


@router.get("/faces")
async def list_faces():
    """Proxy: list enrolled faces from face-recognizer."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{ctx.FACE_API_URL}/api/faces", timeout=5)
            return resp.json()
    except Exception as e:
        ctx.logger.warning(f"Face API unavailable: {e}")
        return JSONResponse(status_code=503, content={"error": "Face recognizer not available"})


@router.post("/faces/preview")
async def preview_face():
    """Proxy: preview face crop from face-recognizer (no enrollment)."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{ctx.FACE_API_URL}/api/faces/preview", timeout=10)
            return JSONResponse(status_code=resp.status_code, content=resp.json())
    except Exception as e:
        ctx.logger.warning(f"Face preview failed: {e}")
        return JSONResponse(status_code=503, content={"error": "Face recognizer not available"})


@router.post("/faces/enroll")
async def enroll_face(data: dict):
    """Proxy: enroll a new face via face-recognizer."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{ctx.FACE_API_URL}/api/faces/enroll",
                json=data,
                timeout=10,
            )
            result = resp.json()

            # Send Telegram notification on successful enrollment
            if resp.status_code == 200:
                from routes.notifications import notify_face_enrolled
                name = data.get("name", "Unknown")
                # Try to get the face photo for the notification
                photo_bytes = None
                try:
                    face_id = result.get("face_id")
                    if face_id:
                        photo_resp = await client.get(
                            f"{ctx.FACE_API_URL}/api/faces/{face_id}/photo", timeout=5
                        )
                        if photo_resp.status_code == 200:
                            photo_bytes = photo_resp.content
                except Exception:
                    pass  # Photo is optional for notification
                await notify_face_enrolled(name, photo_bytes)

            return JSONResponse(status_code=resp.status_code, content=result)
    except Exception as e:
        ctx.logger.warning(f"Face enrollment failed: {e}")
        return JSONResponse(status_code=503, content={"error": "Face recognizer not available"})


@router.delete("/faces/{face_id}")
async def delete_face(face_id: int):
    """Proxy: delete an enrolled face via face-recognizer."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.delete(f"{ctx.FACE_API_URL}/api/faces/{face_id}", timeout=5)
            return resp.json()
    except Exception as e:
        ctx.logger.warning(f"Face deletion failed: {e}")
        return JSONResponse(status_code=503, content={"error": "Face recognizer not available"})


@router.get("/faces/{face_id}/photo")
async def get_face_photo(face_id: int):
    """Proxy: get face thumbnail from face-recognizer."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{ctx.FACE_API_URL}/api/faces/{face_id}/photo", timeout=5)
            if resp.status_code == 200:
                return Response(content=resp.content, media_type="image/jpeg")
            return JSONResponse(status_code=404, content={"error": "Face not found"})
    except Exception as e:
        return JSONResponse(status_code=503, content={"error": "Face recognizer not available"})
