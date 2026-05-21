"""
routes/notifications/endpoints.py — FastAPI REST endpoints for the notifications router.

Owns the `router` object that server.py wires into the dashboard app via
`from routes.notifications import router as notifications_router`. Keeps
HTTP concerns out of the alert/transport code.
"""


from fastapi import APIRouter
from fastapi.responses import JSONResponse

import routes as ctx

from ._shared import (
    is_configured,
    _esc,
    _now_str,
    _get_cooldown,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)
from .telegram_api import send_text, send_photo
from .frame import get_latest_frame

router = APIRouter(prefix="/api", tags=["notifications"])

@router.get("/notifications/status")
async def notification_status():
    """Check if Telegram notifications are configured."""
    return {
        "configured": is_configured(),
        "has_token": bool(TELEGRAM_BOT_TOKEN),
        "has_chat_id": bool(TELEGRAM_CHAT_ID),
        "rate_limit_seconds": _get_cooldown("notify_cooldown", 60),
        "feedback_enabled": True,
    }


@router.post("/notifications/test")
async def test_notification(camera: str = ""):
    """Send a test notification to Telegram with a camera snapshot.

    Pass ?camera=<id> to use a specific camera's frame + label it in the
    caption (so a test from cam2's detail view actually uses cam2's frame
    and the alert clearly says it came from cam2).
    """
    if not is_configured():
        return JSONResponse(
            status_code=400,
            content={"error": "Telegram not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env"},
        )

    # Resolve which camera the test is for. Empty/unknown \u2192 primary.
    cam_id = (camera or "").strip() or ctx.CAMERA_ID
    cam_name = cam_id
    try:
        import cameras as _cam_reg
        for c in _cam_reg.list_enabled_cameras():
            if c.get("id") == cam_id:
                cam_name = c.get("name") or cam_id
                break
    except Exception:
        pass

    caption = (
        f"\U0001f9ea <b>Test Notification</b>\n"
        f"\u2022 Camera: {_esc(cam_name)} ({_esc(cam_id)})\n"
        f"\u2022 Time: {_now_str()}\n"
        f"\u2022 Status: \u2705 Notifications working!"
    )

    frame = get_latest_frame(camera_id=cam_id)
    if frame:
        ok = await send_photo(frame, caption)
    else:
        ok = await send_text(caption + f"\n\n(No frame available for {_esc(cam_id)} \u2014 camera may be offline)")

    if ok:
        return {"status": "sent", "message": f"Test sent for {cam_name} ({cam_id})", "camera": cam_id}
    else:
        return JSONResponse(status_code=500, content={"error": "Failed to send. Check bot token and chat ID"})
