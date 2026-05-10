"""
routes/notifications.py — Telegram notification & alert endpoints.

PURPOSE:
    Send Telegram notifications with photos for:
    - Person detection events (with camera snapshot + inline feedback buttons)
    - Person identification events (with camera snapshot + inline feedback buttons)
    - Vehicle idle events (with camera snapshot + inline feedback buttons)
    - Face enrollment (with face photo)
    - Manual test notifications (with camera snapshot)

    Self-learning feedback loop (Phase 6.5):
    - Every alert includes inline keyboard: ✅ Real | ❌ False | 👤 Name
    - Background poller receives button taps via Telegram getUpdates API
    - Verdicts are stored in feedback_db.py → auto-generates suppression rules

ENDPOINTS:
    POST /api/notifications/test    — Send a test notification
    GET  /api/notifications/status  — Check if Telegram is configured

BOT COMMANDS:
    Moved to routes/bot_commands.py

SECURITY:
    - Bot token + chat ID kept in .env, passed via docker-compose
    - All API calls use HTTPS to Telegram servers
    - Rate-limited to prevent notification spam
"""

import os
import io
import json
import time
import base64
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import redis
import httpx
import numpy as np
import cv2
from fastapi import APIRouter
from fastapi.responses import JSONResponse

import routes as ctx

router = APIRouter(prefix="/api", tags=["notifications"])

# ---------------------------------------------------------------------------
# Telegram config — read from environment
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# Security — seed users from env var (migrated to Redis at startup)
# Comma-separated in .env, e.g. TELEGRAM_ALLOWED_USERS=1004507388,123456789
TELEGRAM_ALLOWED_USERS: set[int] = {
    int(uid.strip())
    for uid in os.getenv("TELEGRAM_ALLOWED_USERS", "").split(",")
    if uid.strip().isdigit()
}


# Timezone — from env (handles EST/EDT automatically via zoneinfo)
TZ_LOCAL = ZoneInfo(os.getenv("LOCATION_TIMEZONE", "America/Toronto"))

# Vision model config — for AI scene analysis on detection snapshots
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://ollama:11434")
VISION_MODEL = os.getenv("VISION_MODEL", "minicpm-v")

# Rate limiting — reads cooldown from Redis config, falls back to defaults
_last_person_notification = 0.0


def _get_cooldown(key: str, default: int) -> int:
    """Read a cooldown value from Redis config, falling back to default."""
    try:
        val = ctx.r.hget(ctx.CONFIG_KEY, key)
        if val:
            return max(10, int(float(val)))  # Floor at 10s to prevent spam
    except Exception:
        pass
    return default

# Redis config — for binary frame reads
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

logger = logging.getLogger("dashboard.notifications")


def _now_str() -> str:
    """Get the current time formatted in local timezone."""
    return datetime.now(TZ_LOCAL).strftime("%I:%M:%S %p")


def is_configured() -> bool:
    """Check if Telegram bot token and chat ID are both set."""
    return bool(TELEGRAM_BOT_TOKEN) and bool(TELEGRAM_CHAT_ID)


def _is_authorized(user_id: int | None, chat_id: int | None) -> bool:
    """
    Security gate — checks if user is approved in Redis.

    Falls back to TELEGRAM_ALLOWED_USERS env var + TELEGRAM_CHAT_ID
    if Redis hash is not yet populated (bootstrap compatibility).
    """
    if not user_id or not chat_id:
        return False

    uid_str = str(user_id)

    # Primary: check Redis hash
    if ctx.TELEGRAM_USERS_KEY and ctx.r:
        if ctx.r.hexists(ctx.TELEGRAM_USERS_KEY, uid_str):
            return True

    # Fallback: env var whitelist + chat ID check (pre-migration)
    if TELEGRAM_ALLOWED_USERS and user_id in TELEGRAM_ALLOWED_USERS:
        if str(chat_id) == TELEGRAM_CHAT_ID:
            return True

    return False


def _get_all_chat_ids() -> list[str]:
    """
    Get chat IDs for ALL approved Telegram users.
    Used for broadcasting system alerts (person detected, vehicle idle, etc.).
    Falls back to TELEGRAM_CHAT_ID if no users in Redis.
    """
    chat_ids = []
    if ctx.TELEGRAM_USERS_KEY and ctx.r:
        raw = ctx.r.hgetall(ctx.TELEGRAM_USERS_KEY)
        for uid_bytes, meta_bytes in raw.items():
            meta = meta_bytes if isinstance(meta_bytes, str) else meta_bytes.decode()
            try:
                data = json.loads(meta)
                cid = data.get("chat_id", "")
                if cid:
                    chat_ids.append(str(cid))
            except (json.JSONDecodeError, TypeError):
                pass
    # Fallback: primary admin chat
    if not chat_ids and TELEGRAM_CHAT_ID:
        chat_ids.append(TELEGRAM_CHAT_ID)
    return chat_ids


# ---------------------------------------------------------------------------
# Telegram API helpers
# ---------------------------------------------------------------------------
async def send_text(message: str, chat_id: str = "") -> bool:
    """Send a plain text message to a specific Telegram chat."""
    if not is_configured():
        logger.warning("Telegram not configured — skipping notification")
        return False
    target = chat_id or TELEGRAM_CHAT_ID
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{TELEGRAM_API}/sendMessage",
                json={"chat_id": target, "text": message, "parse_mode": "HTML"},
                timeout=10,
            )
            if resp.status_code != 200:
                logger.warning(f"Telegram sendMessage failed: {resp.status_code} {resp.text}")
                return False
            return True
    except Exception as e:
        logger.warning(f"Telegram sendMessage error: {e}")
        return False


async def broadcast_text(message: str) -> bool:
    """Send a text message to ALL approved users."""
    chat_ids = _get_all_chat_ids()
    if not chat_ids:
        return False
    results = []
    for cid in chat_ids:
        results.append(await send_text(message, chat_id=cid))
    return any(results)


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
            resp = await client.post(
                f"{TELEGRAM_API}/sendPhoto",
                data=data,
                files={"photo": ("snapshot.jpg", photo_bytes, "image/jpeg")},
                timeout=15,
            )
            if resp.status_code != 200:
                logger.warning(f"Telegram sendPhoto failed: {resp.status_code} {resp.text}")
                return 0
            result = resp.json().get("result", {})
            return result.get("message_id", 0)
    except Exception as e:
        logger.warning(f"Telegram sendPhoto error: {e}")
        return 0


async def broadcast_photo(photo_bytes: bytes, caption: str = "",
                          reply_markup: dict = None) -> int:
    """Send a photo to ALL approved users. Returns first message_id."""
    chat_ids = _get_all_chat_ids()
    if not chat_ids:
        return 0
    first_msg_id = 0
    for cid in chat_ids:
        mid = await send_photo(photo_bytes, caption,
                               reply_markup=reply_markup, chat_id=cid)
        if not first_msg_id and mid:
            first_msg_id = mid

    # Increment Prometheus notification counter
    if first_msg_id:
        try:
            from routes.metrics import vl_notifications_total
            # Determine notification type from caption keywords
            if "Vehicle" in caption:
                vl_notifications_total.labels(type="vehicle").inc()
            elif "Identified" in caption:
                vl_notifications_total.labels(type="identified").inc()
            else:
                vl_notifications_total.labels(type="person").inc()
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


# ---------------------------------------------------------------------------
# Snapshot helper — grab latest frame from Redis (BINARY client)
# Prefers HD frame for higher quality, falls back to sub-stream.
# ---------------------------------------------------------------------------
def get_latest_frame() -> bytes | None:
    """
    Get the latest JPEG frame from Redis.
    Tries the HD frame first (frame_hd:{camera_id}), then falls
    back to the sub-stream (frames:{camera_id}).
    Uses a SEPARATE binary Redis client (decode_responses=False)
    because frame data is raw JPEG bytes.
    """
    try:
        r_bin = ctx.r_bin

        # --- Try HD frame first (clearer image) ---
        if ctx.HD_FRAME_KEY:
            hd_bytes = r_bin.get(ctx.HD_FRAME_KEY.encode())
            if hd_bytes and len(hd_bytes) > 100:
                return hd_bytes

        # --- Fall back to sub-stream ---
        entries = r_bin.xrevrange(ctx.FRAME_STREAM.encode(), count=1)
        if entries:
            _, data = entries[0]
            frame = data.get(b"frame")
            if frame and len(frame) > 100:  # Sanity check — real JPEG is >100 bytes
                return frame
            logger.warning(f"Frame data too small or missing: {len(frame) if frame else 0} bytes")
    except Exception as e:
        logger.warning(f"Failed to get latest frame: {e}")
    return None


def get_sd_frame() -> bytes | None:
    """Get the sub-stream (SD) frame only — used for bbox coordinate reference."""
    try:
        r_bin = ctx.r_bin
        entries = r_bin.xrevrange(ctx.FRAME_STREAM.encode(), count=1)
        if entries:
            _, data = entries[0]
            frame = data.get(b"frame")
            if frame and len(frame) > 100:
                return frame
    except Exception:
        pass
    return None


def draw_bbox_on_frame(frame_bytes: bytes, bbox_json: str,
                       label: str = "",
                       color: tuple = (0, 255, 0)) -> bytes:
    """
    Draw a bounding box highlight on a JPEG frame.
    If the frame is HD, scales bbox coords from sub-stream dimensions.
    Returns the modified JPEG bytes.
    """
    try:
        bbox = json.loads(bbox_json) if isinstance(bbox_json, str) else bbox_json
        if not bbox or len(bbox) != 4:
            return frame_bytes

        np_arr = np.frombuffer(frame_bytes, np.uint8)
        img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        if img is None:
            return frame_bytes

        x1, y1, x2, y2 = [float(v) for v in bbox]
        snap_h, snap_w = img.shape[:2]

        # If snapshot is HD (>= 1000px wide), scale bbox from SD coords
        if snap_w >= 1000:
            sd_frame = get_sd_frame()
            if sd_frame:
                sd_arr = np.frombuffer(sd_frame, np.uint8)
                sd_img = cv2.imdecode(sd_arr, cv2.IMREAD_COLOR)
                if sd_img is not None:
                    sd_h, sd_w = sd_img.shape[:2]
                    sx = snap_w / sd_w
                    sy = snap_h / sd_h
                    x1, y1, x2, y2 = x1 * sx, y1 * sy, x2 * sx, y2 * sy

        ix1, iy1, ix2, iy2 = int(x1), int(y1), int(x2), int(y2)
        cv2.rectangle(img, (ix1, iy1), (ix2, iy2), color, 3)
        if label:
            cv2.putText(img, label, (ix1, iy1 - 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)

        _, encoded = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])
        return encoded.tobytes()
    except Exception:
        return frame_bytes


# ---------------------------------------------------------------------------
# AI Scene Analysis — vision model describes the snapshot
# ---------------------------------------------------------------------------

_PERSON_PROMPT = (
    "You are a private, local security camera appearance logger. "
    "No data leaves this device. You are NOT identifying anyone — "
    "just providing an objective physical description for the property owner's log. "
    "Describe the person visible in this image in 2-3 concise sentences. "
    "Include: clothing (color, type), build, hair style/color, "
    "accessories (bag, hat, glasses, etc.), posture, and direction of "
    "movement if discernible. Note anything unusual about their behavior. "
    "Be factual and brief — this goes into a local alert log."
)

_VEHICLE_PROMPT = (
    "You are a private, local security camera logger. "
    "Describe the vehicle in this image in 2-3 concise sentences. "
    "Include: vehicle type, color, approximate make/model if visible, "
    "any readable text or plates, and position relative to the property. "
    "Note anything unusual. Be factual and brief — this is for a local log."
)


async def describe_scene(photo_bytes: bytes,
                         prompt: str = _PERSON_PROMPT,
                         timeout: float = 20.0) -> str:
    """
    Send a snapshot to the MiniCPM-V vision model via Ollama for analysis.

    Returns a text description of the scene, or empty string on failure.
    Runs the (blocking) Ollama call in a thread to avoid stalling the
    asyncio event loop.

    The vision model auto-loads into VRAM on first call and unloads after
    its keep_alive window (default 5 min), so it doesn't compete with
    Qwen 3 14B during idle periods.
    """
    def _call_vision_model() -> str:
        try:
            import ollama as ollama_lib
            client = ollama_lib.Client(host=OLLAMA_HOST)
            response = client.chat(
                model=VISION_MODEL,
                messages=[{
                    "role": "user",
                    "content": prompt,
                    "images": [photo_bytes],
                }],
                options={"num_predict": 200},
                keep_alive="5m",
            )
            text = response.message.content.strip()
            # Strip any <think>...</think> tags from reasoning models
            import re
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
            return text
        except Exception as e:
            logger.warning(f"Vision model ({VISION_MODEL}) failed: {e}")
            return ""

    try:
        description = await asyncio.wait_for(
            asyncio.to_thread(_call_vision_model),
            timeout=timeout,
        )
        if description:
            logger.info(f"AI scene analysis ({len(description)} chars): {description[:80]}...")
        return description
    except asyncio.TimeoutError:
        logger.warning(f"Vision model timed out after {timeout}s")
        return ""
    except Exception as e:
        logger.warning(f"describe_scene error: {e}")
        return ""


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
            resp = await client.post(
                f"{TELEGRAM_API}/sendVideo",
                data=data,
                files={"video": ("clip.mp4", video_bytes, "video/mp4")},
                timeout=30,
            )
            if resp.status_code != 200:
                logger.warning(f"Telegram sendVideo failed: {resp.status_code} {resp.text}")
                return 0
            result = resp.json().get("result", {})
            return result.get("message_id", 0)
    except Exception as e:
        logger.warning(f"Telegram sendVideo error: {e}")
        return 0


async def broadcast_video(video_bytes: bytes, caption: str = "") -> int:
    """Send a video to ALL approved users. Returns first message_id."""
    chat_ids = _get_all_chat_ids()
    if not chat_ids:
        return 0
    first_msg_id = 0
    for cid in chat_ids:
        mid = await send_video(video_bytes, caption, chat_id=cid)
        if not first_msg_id and mid:
            first_msg_id = mid
    return first_msg_id


def build_clip(duration: float = 5.0, fps: int = 10) -> bytes | None:
    """
    Capture frames from the Redis stream and encode as MP4 clip.
    Collects frames for `duration` seconds using xread to get unique frames.
    Returns MP4 bytes or None on failure.
    """
    import cv2
    import numpy as np
    import tempfile
    import time as _time

    try:
        r_bin = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=False)
        frames = []
        target_count = int(duration * fps)
        start = _time.monotonic()
        stream_key = ctx.FRAME_STREAM.encode()

        # Get the latest stream ID as our starting point
        latest = r_bin.xrevrange(stream_key, count=1)
        if not latest:
            logger.warning("build_clip: no frames in stream")
            return None
        last_id = latest[0][0]  # Start AFTER this frame

        # Grab the first frame immediately
        _, data = latest[0]
        frame_bytes = data.get(b"frame")
        if frame_bytes and len(frame_bytes) > 100:
            nparr = np.frombuffer(frame_bytes, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img is not None:
                frames.append(img)

        # Read NEW frames using xread(block=...) — only returns genuinely new entries
        while len(frames) < target_count:
            elapsed = _time.monotonic() - start
            if elapsed > duration + 3:
                break  # Safety timeout

            # Block up to 500ms waiting for a new frame
            result = r_bin.xread({stream_key: last_id}, count=5, block=500)
            if not result:
                continue  # No new frames yet, retry

            for _, entries in result:
                for entry_id, data in entries:
                    last_id = entry_id
                    frame_bytes = data.get(b"frame")
                    if frame_bytes and len(frame_bytes) > 100:
                        nparr = np.frombuffer(frame_bytes, np.uint8)
                        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                        if img is not None:
                            frames.append(img)
                    if len(frames) >= target_count:
                        break

        if len(frames) < 5:
            logger.warning(f"build_clip: only captured {len(frames)} frames, need at least 5")
            return None

        # Calculate actual FPS from capture timing
        actual_duration = _time.monotonic() - start
        actual_fps = len(frames) / actual_duration if actual_duration > 0 else fps

        # Encode to MP4 using actual capture FPS so playback speed matches reality
        h, w = frames[0].shape[:2]
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp_path = tmp.name

        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(tmp_path, fourcc, actual_fps, (w, h))
        for f in frames:
            writer.write(f)
        writer.release()

        # Re-encode to H.264 for Telegram/browser compatibility
        # OpenCV's mp4v (MPEG-4 Part 2) won't play inline in Telegram
        import os
        import subprocess
        h264_path = tmp_path + ".h264.mp4"
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", tmp_path,
                 "-c:v", "libx264", "-preset", "ultrafast",
                 "-movflags", "+faststart", "-an", h264_path],
                capture_output=True, timeout=30,
            )
            os.unlink(tmp_path)
            with open(h264_path, "rb") as f:
                video_bytes = f.read()
            os.unlink(h264_path)
        except Exception as e:
            logger.warning(f"build_clip: ffmpeg re-encode failed ({e}), using raw mp4v")
            with open(tmp_path, "rb") as f:
                video_bytes = f.read()
            os.unlink(tmp_path)

        if len(video_bytes) < 1000:
            logger.warning(f"build_clip: video too small ({len(video_bytes)} bytes)")
            return None

        logger.info(f"build_clip: captured {len(frames)} unique frames in {actual_duration:.1f}s ({actual_fps:.1f} fps)")
        return video_bytes

    except Exception as e:
        logger.warning(f"build_clip error: {e}")
        return None


# ---------------------------------------------------------------------------
# Notify on person detection (rate-limited)
# ---------------------------------------------------------------------------
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
    global _last_person_notification

    if not is_configured():
        return 0

    now = time.time()
    cooldown = _get_cooldown("notify_cooldown", 60)
    if now - _last_person_notification < cooldown:
        remaining = cooldown - (now - _last_person_notification)
        logger.debug(f"Person notification rate-limited ({remaining:.0f}s remaining in {cooldown}s cooldown)")
        return 0  # Rate limited

    # Check suppression BEFORE updating the rate-limit timer.
    # If suppressed, we don't want to burn the cooldown window.
    identity = event_data.get("identity_name", "")
    zone = event_data.get("zone", "")
    action = event_data.get("action", "")
    time_period = event_data.get("time_period", "")
    confidence = float(event_data.get("confidence", "0") or "0")


    _last_person_notification = now

    person_id = event_data.get("person_id", "unknown")
    name = identity if identity else person_id
    parts = [f"\U0001f6a8 <b>Person Detected</b>"]
    parts.append(f"\u2022 Who: {name}")
    if zone:
        parts.append(f"\u2022 Zone: {zone}")
    if action:
        parts.append(f"\u2022 Action: {action}")
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
        msg_id = await broadcast_photo(frame, caption)
    else:
        await broadcast_text(caption)
        msg_id = 0



    return msg_id


# ---------------------------------------------------------------------------
# Notify on person identification (NOT rate-limited — always important)
# ---------------------------------------------------------------------------
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
    time_period = event_data.get("time_period", "")
    confidence = float(event_data.get("confidence", "0") or "0")

    if not identity_name:
        return 0  # Skip if no name was identified



    parts = [f"\U0001f464 <b>Person Identified</b>"]
    parts.append(f"\u2022 Name: {identity_name}")
    parts.append(f"\u2022 Tracker ID: {person_id}")
    if zone:
        parts.append(f"\u2022 Zone: {zone}")
    if action:
        parts.append(f"\u2022 Action: {action}")
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
        msg_id = await broadcast_photo(frame, caption)
    else:
        await broadcast_text(caption)
        msg_id = 0

    return msg_id


# ---------------------------------------------------------------------------
# Notify on vehicle idle (rate-limited separately from person notifications)
# ---------------------------------------------------------------------------
_last_vehicle_idle_notification = 0.0


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
    global _last_vehicle_idle_notification

    if not is_configured():
        return 0

    now = time.time()
    cooldown = _get_cooldown("vehicle_cooldown", 60)
    if now - _last_vehicle_idle_notification < cooldown:
        remaining = cooldown - (now - _last_vehicle_idle_notification)
        logger.debug(f"Vehicle idle notification rate-limited ({remaining:.0f}s remaining in {cooldown}s cooldown)")
        return 0  # Rate limited

    vehicle_class = event_data.get("vehicle_class", "vehicle")
    zone = event_data.get("zone", "")
    time_period = event_data.get("time_period", "")
    duration_raw = float(event_data.get("duration", "0") or "0")
    confidence = event_data.get("vehicle_confidence", "")



    _last_vehicle_idle_notification = now

    # Format duration as human-readable string
    if duration_raw >= 3600:
        duration_str = f"{duration_raw / 3600:.1f} hours"
    elif duration_raw >= 60:
        duration_str = f"{duration_raw / 60:.0f} min"
    else:
        duration_str = f"{duration_raw:.0f}s"

    parts = [f"\U0001f697 <b>Vehicle Idling</b>"]
    parts.append(f"\u2022 Type: {vehicle_class}")
    if zone:
        parts.append(f"\u2022 Zone: {zone}")
    parts.append(f"\u2022 Stationary: {duration_str}")
    if confidence:
        parts.append(f"\u2022 Confidence: {confidence}")
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
        msg_id = await broadcast_photo(frame, caption)
    else:
        await broadcast_text(caption)
        msg_id = 0



    # Note: No follow-up clip for vehicle idle — the snapshot with bbox is the
    # useful artifact. A live clip captured now would show the current scene,
    # not when the vehicle was first detected (it may have already left).

    return msg_id


# ---------------------------------------------------------------------------
# Notify on face enrollment
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------
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
async def test_notification():
    """Send a test notification to Telegram with a camera snapshot."""
    if not is_configured():
        return JSONResponse(
            status_code=400,
            content={"error": "Telegram not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env"},
        )

    caption = (
        f"\U0001f9ea <b>Test Notification</b>\n"
        f"\u2022 Source: Vision Labs Dashboard\n"
        f"\u2022 Time: {_now_str()}\n"
        f"\u2022 Status: \u2705 Notifications working!"
    )

    frame = get_latest_frame()
    if frame:
        ok = await send_photo(frame, caption)
    else:
        ok = await send_text(caption + "\n\n(No camera frame available)")

    if ok:
        return {"status": "sent", "message": "Test notification sent to Telegram"}
    else:
        return JSONResponse(status_code=500, content={"error": "Failed to send. Check bot token and chat ID"})



