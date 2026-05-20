"""
routes/notifications/telegram_api.py — low-level Telegram API send/broadcast wrappers.

Every outbound HTTP call to api.telegram.org goes through one of these
helpers. Handles 429 retry-after, sets parse_mode=HTML, and broadcasts
to all approved chats in parallel.
"""

import json
import asyncio
import logging

import httpx

import routes as ctx

from ._shared import (
    logger,
    is_configured,
    TELEGRAM_API,
    TELEGRAM_CHAT_ID,
    _redact_token,
    _get_all_chat_ids,
)

async def _handle_429(resp: "httpx.Response", call_name: str) -> float:
    """If `resp` is a 429 Too Many Requests, parse Telegram's retry_after
    and return the wait seconds (cap 30 so we don't sleep forever on a
    misconfigured chat). Otherwise return 0.
    """
    if resp.status_code != 429:
        return 0.0
    try:
        body = resp.json()
        wait = float(body.get("parameters", {}).get("retry_after", 1.0))
    except Exception:
        wait = 1.0
    wait = min(max(0.5, wait), 30.0)
    logger.warning(
        f"Telegram 429 on {call_name}: retry_after={wait:.1f}s (will retry once)"
    )
    return wait


async def send_text(message: str, chat_id: str = "",
                    reply_markup: dict | None = None) -> bool:
    """Send a plain text message to a specific Telegram chat.
    `reply_markup` accepts a Telegram InlineKeyboardMarkup dict (used to
    attach tap-to-pick buttons under the message)."""
    if not is_configured():
        logger.warning("Telegram not configured — skipping notification")
        return False
    target = chat_id or TELEGRAM_CHAT_ID
    try:
        payload = {"chat_id": target, "text": message, "parse_mode": "HTML"}
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        async with httpx.AsyncClient() as client:
            for attempt in (1, 2):
                resp = await client.post(
                    f"{TELEGRAM_API}/sendMessage",
                    json=payload,
                    timeout=10,
                )
                wait = await _handle_429(resp, "sendMessage")
                if wait and attempt == 1:
                    await asyncio.sleep(wait)
                    continue
                if resp.status_code != 200:
                    logger.warning(f"Telegram sendMessage failed: {resp.status_code} {_redact_token(resp.text)}")
                    return False
                return True
        return False
    except Exception as e:
        logger.warning(f"Telegram sendMessage error: {e}")
        return False


async def broadcast_text(message: str) -> bool:
    """Send a text message to ALL approved users — concurrently."""
    chat_ids = _get_all_chat_ids()
    if not chat_ids:
        return False
    # Was serial (`for cid: await send_text`), so N users × per-user-latency
    # blocked the event loop. Now parallel: total = max(per-user-latency).
    results = await asyncio.gather(
        *(send_text(message, chat_id=cid) for cid in chat_ids),
        return_exceptions=True,
    )
    return any(r is True for r in results)


async def send_photo(photo_bytes: bytes, caption: str = "",
                     reply_markup: dict = None,
                     chat_id: str = "") -> int:
    """
    Send a photo with optional caption to a specific Telegram chat.
    Returns the Telegram message_id (0 on failure).
    """
    if not is_configured():
        logger.warning("Telegram not configured — skipping photo notification")
        return 0
    target = chat_id or TELEGRAM_CHAT_ID
    try:
        data = {"chat_id": target, "caption": caption, "parse_mode": "HTML"}
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
        async with httpx.AsyncClient() as client:
            for attempt in (1, 2):
                resp = await client.post(
                    f"{TELEGRAM_API}/sendPhoto",
                    data=data,
                    files={"photo": ("snapshot.jpg", photo_bytes, "image/jpeg")},
                    timeout=15,
                )
                wait = await _handle_429(resp, "sendPhoto")
                if wait and attempt == 1:
                    await asyncio.sleep(wait)
                    continue
                if resp.status_code != 200:
                    logger.warning(f"Telegram sendPhoto failed: {resp.status_code} {_redact_token(resp.text)}")
                    return 0
                result = resp.json().get("result", {})
                return result.get("message_id", 0)
        return 0
    except Exception as e:
        logger.warning(f"Telegram sendPhoto error: {e}")
        return 0


async def broadcast_photo(photo_bytes: bytes, caption: str = "",
                          reply_markup: dict = None,
                          camera_id: str = "") -> int:
    """Send a photo to ALL approved users concurrently. Returns first
    successful message_id (0 if every send failed).

    `camera_id` is used to label the Prometheus notification counter
    so Grafana can chart "alerts per camera". Callers should pass the
    event's camera_id from event_data; falls back to dashboard primary
    if not provided.
    """
    chat_ids = _get_all_chat_ids()
    if not chat_ids:
        return 0
    results = await asyncio.gather(
        *(send_photo(photo_bytes, caption, reply_markup=reply_markup, chat_id=cid)
          for cid in chat_ids),
        return_exceptions=True,
    )
    first_msg_id = 0
    for r in results:
        if isinstance(r, int) and r and not first_msg_id:
            first_msg_id = r
            break

    # Increment Prometheus notification counter
    if first_msg_id:
        try:
            from routes.metrics import vl_notifications_total
            # Determine notification type from caption keywords
            if "Vehicle" in caption:
                ntype = "vehicle"
            elif "Identified" in caption:
                ntype = "identified"
            else:
                ntype = "person"
            cam = camera_id or ctx.CAMERA_ID
            vl_notifications_total.labels(camera=cam, type=ntype).inc()
        except Exception:
            pass  # Metrics not loaded yet during startup

    return first_msg_id


async def edit_message_buttons(message_id: int, text: str,
                                chat_id: str = "") -> bool:
    """
    Replace the inline keyboard on a sent message with a confirmation text.
    Called after the user taps a verdict button.
    """
    if not is_configured():
        return False
    target = chat_id or TELEGRAM_CHAT_ID
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{TELEGRAM_API}/editMessageReplyMarkup",
                json={
                    "chat_id": target,
                    "message_id": message_id,
                    "reply_markup": {"inline_keyboard": [
                        [{"text": text, "callback_data": "noop"}]
                    ]},
                },
                timeout=10,
            )
            return resp.status_code == 200
    except Exception as e:
        logger.warning(f"editMessageReplyMarkup error: {e}")
        return False


async def answer_callback_query(callback_query_id: str,
                                 text: str = "Recorded!") -> bool:
    """Acknowledge a Telegram callback query (removes loading spinner)."""
    if not is_configured():
        return False
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{TELEGRAM_API}/answerCallbackQuery",
                json={"callback_query_id": callback_query_id, "text": text},
                timeout=10,
            )
            return resp.status_code == 200
    except Exception as e:
        logger.warning(f"answerCallbackQuery error: {e}")
        return False


async def send_video(video_bytes: bytes, caption: str = "",
                     chat_id: str = "") -> int:
    """
    Send a video (MP4) with optional caption to Telegram.
    Returns the Telegram message_id (0 on failure).
    """
    if not is_configured():
        logger.warning("Telegram not configured — skipping video notification")
        return 0
    target = chat_id or TELEGRAM_CHAT_ID
    try:
        data = {"chat_id": target, "caption": caption, "parse_mode": "HTML"}
        async with httpx.AsyncClient() as client:
            for attempt in (1, 2):
                resp = await client.post(
                    f"{TELEGRAM_API}/sendVideo",
                    data=data,
                    files={"video": ("clip.mp4", video_bytes, "video/mp4")},
                    timeout=30,
                )
                wait = await _handle_429(resp, "sendVideo")
                if wait and attempt == 1:
                    await asyncio.sleep(wait)
                    continue
                if resp.status_code != 200:
                    logger.warning(f"Telegram sendVideo failed: {resp.status_code} {_redact_token(resp.text)}")
                    return 0
                result = resp.json().get("result", {})
                return result.get("message_id", 0)
        return 0
    except Exception as e:
        logger.warning(f"Telegram sendVideo error: {e}")
        return 0


async def broadcast_video(video_bytes: bytes, caption: str = "") -> int:
    """Send a video to ALL approved users concurrently. Returns first
    successful message_id (0 if every send failed)."""
    chat_ids = _get_all_chat_ids()
    if not chat_ids:
        return 0
    results = await asyncio.gather(
        *(send_video(video_bytes, caption, chat_id=cid) for cid in chat_ids),
        return_exceptions=True,
    )
    for r in results:
        if isinstance(r, int) and r:
            return r
    return 0
