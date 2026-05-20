"""
routes/notifications/ — split package (was notifications.py monolith).

Backward-compat surface:
    from routes.notifications import (
        router,                                     # FastAPI router
        send_text, send_photo, send_video,          # raw senders
        broadcast_text, broadcast_photo,            # fan-out senders
        edit_message_buttons, answer_callback_query,
        is_configured, _is_authorized,              # auth/config
        get_latest_frame, get_sd_frame, build_clip, # frame helpers
        describe_scene,                             # vision LLM
        notify_person_detected, notify_person_identified,
        notify_vehicle_idle, notify_face_enrolled,  # high-level alerts
        _esc, _now_str,                             # caption helpers
        TELEGRAM_API, TELEGRAM_CHAT_ID,
        TELEGRAM_ALLOWED_USERS,
        REDIS_HOST, REDIS_PORT,
    )

Internal layout:
    _shared.py      — config, _esc, cooldown helpers, auth gate, broadcast list
    telegram_api.py — send_*/broadcast_*/edit/answer wrappers
    frame.py        — get_latest_frame, get_sd_frame, draw_bbox_on_frame, build_clip
    scene.py        — MiniCPM-V describe_scene + prompts
    alerts.py       — notify_person/identified/vehicle/face_enrolled
    endpoints.py    — REST /api/notifications/{status,test} + router
"""

from ._shared import (
    _esc, _now_str, is_configured, _is_authorized,
    TELEGRAM_API, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_ALLOWED_USERS,
    REDIS_HOST, REDIS_PORT,
)
from .telegram_api import (
    send_text, send_photo, send_video,
    broadcast_text, broadcast_photo, broadcast_video,
    edit_message_buttons, answer_callback_query,
)
from .frame import get_latest_frame, get_sd_frame, build_clip, draw_bbox_on_frame
from .scene import describe_scene, _PERSON_PROMPT, _VEHICLE_PROMPT
from .alerts import (
    notify_person_detected, notify_person_identified,
    notify_vehicle_idle, notify_face_enrolled,
)
from .endpoints import router

__all__ = [
    "router",
    "send_text", "send_photo", "send_video",
    "broadcast_text", "broadcast_photo", "broadcast_video",
    "edit_message_buttons", "answer_callback_query",
    "is_configured", "_is_authorized",
    "get_latest_frame", "get_sd_frame", "build_clip", "draw_bbox_on_frame",
    "describe_scene",
    "notify_person_detected", "notify_person_identified",
    "notify_vehicle_idle", "notify_face_enrolled",
    "_esc", "_now_str",
    "TELEGRAM_API", "TELEGRAM_CHAT_ID", "TELEGRAM_ALLOWED_USERS",
    "REDIS_HOST", "REDIS_PORT",
]
