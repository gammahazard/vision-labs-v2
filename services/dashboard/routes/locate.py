"""routes/locate.py — proxy to the opt-in locate-anything grounding service.

Gated by ENABLE_LOCATE + LOCATE_API_URL (set by docker-compose.locate.yml).
When disabled, the endpoints 404 and the AI-tab option stays hidden.

ENDPOINTS:
    GET  /api/locate/status — {enabled: bool} so the UI can show/hide the tool
    POST /api/locate        — multipart {image, phrase, [mode]} → grounding result
"""
import httpx
from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import JSONResponse

import routes as ctx

router = APIRouter(prefix="/api", tags=["locate"])


def _enabled() -> bool:
    return bool(ctx.ENABLE_LOCATE and ctx.LOCATE_API_URL)


@router.get("/locate/status")
async def locate_status():
    return {"enabled": _enabled()}


@router.post("/locate")
async def locate(
    image: UploadFile = File(...),
    phrase: str = Form(...),
    mode: str = Form("slow"),
):
    if not _enabled():
        return JSONResponse({"error": "locate feature is not enabled"}, status_code=404)
    phrase = (phrase or "").strip()
    if not phrase:
        return JSONResponse({"error": "phrase required"}, status_code=400)
    try:
        raw = await image.read()
    except Exception:
        return JSONResponse({"error": "could not read image"}, status_code=400)

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{ctx.LOCATE_API_URL}/locate",
                files={"image": (image.filename or "upload.jpg", raw,
                                 image.content_type or "image/jpeg")},
                data={"phrase": phrase, "mode": mode},
                # Generous: first request includes a one-time model load
                # (~10-70s); steady-state slow-mode inference is ~4s.
                timeout=180,
            )
        return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception:
        ctx.logger.exception("locate proxy failed")
        return JSONResponse(
            {"error": "locate service unavailable — is it running?"},
            status_code=502,
        )
