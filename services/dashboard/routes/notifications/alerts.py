"""
routes/notifications/alerts.py — high-level notify_* functions for system events.

These compose the lower-level modules: build a caption, fetch a frame,
run scene analysis, draw bbox, then broadcast via telegram_api. Each
alert type owns its own cooldown semantics (person + vehicle rate-limited;
person_identified + face_enrolled always sent).
"""

import time
import logging

import routes as ctx

from ._shared import (
    logger,
    is_configured,
    _esc,
    _now_str,
    _get_cooldown,
    _get_last_notification,
    _set_last_notification,
)
from .telegram_api import (
    send_text, send_photo,
    broadcast_text, broadcast_photo,
)
from .frame import get_latest_frame, draw_bbox_on_frame
from .scene import describe_scene, _PERSON_PROMPT, _VEHICLE_PROMPT

async def notify_person_detected(event_data: dict,
                                  event_id: str = "",
                                  snapshot_bytes: bytes = None) -> int:
    """
    Send a Telegram notification when a person is detected.
    Rate-limited using notify_cooldown from Redis config (default 60s).
    Returns the Telegram message ID (0 if not sent).

    If snapshot_bytes is provided, uses those bytes for the photo
    instead of grabbing a new live frame. This ensures the photo
    shows the same frame that triggered the detection.
    """
    if not is_configured():
        return 0

    cam = event_data.get("camera_id", "")
    now = time.time()
    cooldown = _get_cooldown("notify_cooldown", 60)
    last_sent = _get_last_notification("person", cam)
    if now - last_sent < cooldown:
        remaining = cooldown - (now - last_sent)
        logger.debug(
            f"Person notification rate-limited on {cam or 'global'} "
            f"({remaining:.0f}s remaining in {cooldown}s cooldown)"
        )
        return 0  # Rate limited

    _set_last_notification("person", now, cam)

    identity = event_data.get("identity_name", "")
    zone = event_data.get("zone", "")
    action = event_data.get("action", "")
    person_id = event_data.get("person_id", "unknown")
    name = identity if identity else person_id
    parts = [f"\U0001f6a8 <b>Person Detected</b>"]
    parts.append(f"\u2022 Who: {_esc(name)}")
    if zone:
        parts.append(f"\u2022 Zone: {_esc(zone)}")
    if action:
        parts.append(f"\u2022 Action: {_esc(action)}")
    parts.append(f"\u2022 Time: {_now_str()}")

    caption = "\n".join(parts)


    # Use provided snapshot bytes, fall back to live frame
    frame = snapshot_bytes if snapshot_bytes else get_latest_frame()
    if frame:
        # AI scene analysis — describe the person before sending
        ai_desc = await describe_scene(frame, prompt=_PERSON_PROMPT)
        if ai_desc:
            caption += f"\n\n\U0001f916 <i>{ai_desc}</i>"
            # Store description in Redis for dashboard/journal access
            if event_id:
                try:
                    ctx.r.setex(
                        f"scene_analysis:{event_id}",
                        86400,  # 24h TTL
                        ai_desc,
                    )
                except Exception:
                    pass

        # Draw bbox highlight on the snapshot if available
        # Use snapshot_bbox (matches saved frame) over bbox (latest tracker position)
        # to avoid bbox/frame timing mismatch when person has moved
        bbox_json = event_data.get("snapshot_bbox", "") or event_data.get("bbox", "")
        if bbox_json:
            frame = draw_bbox_on_frame(frame, bbox_json,
                                       label=name, color=(0, 255, 0))
        msg_id = await broadcast_photo(frame, caption, camera_id=event_data.get("camera_id", ""))
    else:
        await broadcast_text(caption)
        msg_id = 0



    return msg_id


async def notify_person_identified(event_data: dict,
                                    event_id: str = "",
                                    snapshot_bytes: bytes = None) -> int:
    """
    Send a Telegram notification when a person is identified by face recognition.
    This is NOT rate-limited because identification is a significant event.
    Returns the Telegram message ID (0 if not sent).

    If snapshot_bytes is provided, uses those bytes for the photo
    instead of grabbing a new live frame.
    """
    if not is_configured():
        return 0

    person_id = event_data.get("person_id", "unknown")
    identity_name = event_data.get("identity_name", "")
    zone = event_data.get("zone", "")
    action = event_data.get("action", "")

    if not identity_name:
        return 0  # Skip if no name was identified



    parts = [f"\U0001f464 <b>Person Identified</b>"]
    parts.append(f"\u2022 Name: {_esc(identity_name)}")
    parts.append(f"\u2022 Tracker ID: {_esc(person_id)}")
    if zone:
        parts.append(f"\u2022 Zone: {_esc(zone)}")
    if action:
        parts.append(f"\u2022 Action: {_esc(action)}")
    parts.append(f"\u2022 Time: {_now_str()}")

    caption = "\n".join(parts)


    # Use provided snapshot bytes, fall back to live frame
    frame = snapshot_bytes if snapshot_bytes else get_latest_frame()
    if frame:
        # Draw bbox highlight on the snapshot if available
        # Use snapshot_bbox (matches saved frame) over bbox (latest tracker position)
        # to avoid bbox/frame timing mismatch when person has moved
        bbox_json = event_data.get("snapshot_bbox", "") or event_data.get("bbox", "")
        if bbox_json:
            frame = draw_bbox_on_frame(frame, bbox_json,
                                       label=identity_name,
                                       color=(255, 255, 0))
        msg_id = await broadcast_photo(frame, caption, camera_id=event_data.get("camera_id", ""))
    else:
        await broadcast_text(caption)
        msg_id = 0

    return msg_id


async def notify_vehicle_idle(event_data: dict,
                               event_id: str = "",
                               snapshot_bytes: bytes = None) -> int:
    """
    Send a Telegram notification when a vehicle has been idling.
    Sends a photo snapshot immediately, then follows up with a 5-second
    video clip for additional context.
    Rate-limited using vehicle_cooldown from Redis config (default 120s).
    Returns the Telegram message ID (0 if not sent).

    If snapshot_bytes is provided, uses those bytes for the photo
    instead of grabbing a new live frame.
    """
    if not is_configured():
        return 0

    cam = event_data.get("camera_id", "")
    now = time.time()
    cooldown = _get_cooldown("vehicle_cooldown", 60)
    last_sent = _get_last_notification("vehicle_idle", cam)
    if now - last_sent < cooldown:
        remaining = cooldown - (now - last_sent)
        logger.debug(
            f"Vehicle idle notification rate-limited on {cam or 'global'} "
            f"({remaining:.0f}s remaining in {cooldown}s cooldown)"
        )
        return 0  # Rate limited

    _set_last_notification("vehicle_idle", now, cam)

    vehicle_class = event_data.get("vehicle_class", "vehicle")
    zone = event_data.get("zone", "")
    duration_raw = float(event_data.get("duration", "0") or "0")
    confidence = event_data.get("vehicle_confidence", "")

    # Format duration as human-readable string
    if duration_raw >= 3600:
        duration_str = f"{duration_raw / 3600:.1f} hours"
    elif duration_raw >= 60:
        duration_str = f"{duration_raw / 60:.0f} min"
    else:
        duration_str = f"{duration_raw:.0f}s"

    parts = [f"\U0001f697 <b>Vehicle Idling</b>"]
    parts.append(f"\u2022 Type: {_esc(vehicle_class)}")
    if zone:
        parts.append(f"\u2022 Zone: {_esc(zone)}")
    parts.append(f"\u2022 Stationary: {duration_str}")
    if confidence:
        parts.append(f"\u2022 Confidence: {_esc(confidence)}")
    parts.append(f"\u2022 Time: {_now_str()}")

    caption = "\n".join(parts)


    # Use provided snapshot bytes, fall back to live frame
    frame = snapshot_bytes if snapshot_bytes else get_latest_frame()
    if frame:
        # AI scene analysis — describe the vehicle before sending
        ai_desc = await describe_scene(frame, prompt=_VEHICLE_PROMPT)
        if ai_desc:
            caption += f"\n\n\U0001f916 <i>{ai_desc}</i>"
            # Store description in Redis for dashboard/journal access
            if event_id:
                try:
                    ctx.r.setex(
                        f"scene_analysis:{event_id}",
                        86400,  # 24h TTL
                        ai_desc,
                    )
                except Exception:
                    pass

        # Use snapshot_bbox (matches saved frame) when available
        bbox_json = event_data.get("snapshot_bbox", "") or event_data.get("bbox", "")
        if bbox_json:
            frame = draw_bbox_on_frame(frame, bbox_json,
                                       label=vehicle_class, color=(0, 165, 255))
        msg_id = await broadcast_photo(frame, caption, camera_id=event_data.get("camera_id", ""))
    else:
        await broadcast_text(caption)
        msg_id = 0



    # Note: No follow-up clip for vehicle idle — the snapshot with bbox is the
    # useful artifact. A live clip captured now would show the current scene,
    # not when the vehicle was first detected (it may have already left).

    return msg_id


async def notify_face_enrolled(name: str, photo_bytes: bytes | None = None):
    """Send a Telegram notification when a new face is enrolled."""
    if not is_configured():
        return

    caption = f"\U0001f4f7 <b>New Face Enrolled</b>\n\u2022 Name: {name}\n\u2022 Time: {_now_str()}"

    if photo_bytes:
        await send_photo(photo_bytes, caption)
    else:
        # Fall back to camera snapshot
        frame = get_latest_frame()
        if frame:
            await send_photo(frame, caption)
        else:
            await send_text(caption)
