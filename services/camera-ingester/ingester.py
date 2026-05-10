"""
services/camera-ingester/ingester.py — Captures RTSP frames and publishes to Redis.

PURPOSE:
    This is the entry point for all video data in the Vision Labs pipeline.
    It connects to a camera's RTSP stream, decodes each frame, JPEG-encodes it,
    and publishes it to a Redis Stream. Every downstream service (detectors,
    dashboard, recorder) consumes frames from Redis — not from the camera directly.

RELATIONSHIPS:
    - Reads camera credentials from environment variables (set in .env / docker-compose)
    - Publishes to Redis Stream key defined in contracts/streams.py (FRAME_STREAM)
    - Downstream consumers: pose-detector (Phase 2), dashboard (Phase 3)

WHY REDIS STREAMS (not direct RTSP per service):
    - One RTSP connection per camera, not N connections per N services
    - If a detector crashes, the ingester keeps running — no frame loss
    - Redis Streams provide backpressure and history replay
    - Adding a new consumer is zero config — just subscribe to the stream

CONFIG (via environment variables):
    CAMERA_ID       — Unique camera name (e.g., "front_door")
    RTSP_URL        — Full RTSP URL including credentials
    REDIS_HOST      — Redis server hostname (default: localhost)
    REDIS_PORT      — Redis server port (default: 6379)
    TARGET_FPS      — How many frames/sec to publish (default: 5)
    JPEG_QUALITY    — JPEG compression quality 1-100 (default: 80)
    MAX_STREAM_LEN  — Max frames to keep in Redis stream (default: 1000)
"""

import os
import sys
import time
import random
import signal
import logging
import threading

# IMPORTANT: must be set BEFORE cv2 is imported. Setting it after the import
# is silently ignored on some FFmpeg builds, which causes connect() to fall
# back to UDP and hang/disconnect under WSL's NAT. Forcing TCP up-front fixes
# the 30-second stream-timeout death spiral the ingester used to enter when
# the Reolink had a momentary hiccup.
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"

import cv2
import redis

# Import stream key definitions from contracts (single source of truth)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "contracts"))
from streams import (
    FRAME_STREAM as _FRAME_TMPL,
    HD_FRAME_KEY as _HD_TMPL,
    CONFIG_KEY as _CFG_TMPL,
    stream_key,
)

# ---------------------------------------------------------------------------
# Configuration from environment variables
# ---------------------------------------------------------------------------
CAMERA_ID = os.getenv("CAMERA_ID", "front_door")
RTSP_URL = os.getenv("RTSP_URL", "")
RTSP_MAIN_URL = os.getenv("RTSP_MAIN", "")  # High-res main stream for HD viewing
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
TARGET_FPS = int(os.getenv("TARGET_FPS", "5"))
HD_TARGET_FPS = int(os.getenv("HD_TARGET_FPS", "5"))  # Lower FPS for HD to save bandwidth
JPEG_QUALITY = int(os.getenv("JPEG_QUALITY", "80"))
HD_JPEG_QUALITY = int(os.getenv("HD_JPEG_QUALITY", "85"))
MAX_STREAM_LEN = int(os.getenv("MAX_STREAM_LEN", "1000"))

# Redis Stream key — resolved from contracts/streams.py
STREAM_KEY = stream_key(_FRAME_TMPL, camera_id=CAMERA_ID)
HD_FRAME_KEY = stream_key(_HD_TMPL, camera_id=CAMERA_ID)
CONFIG_KEY = stream_key(_CFG_TMPL, camera_id=CAMERA_ID)

# How often to re-read CONFIG_KEY for hot-reloadable settings (target_fps).
# Matches the cadence used by detectors. Doesn't need to be aggressive — tuning
# FPS via the dashboard slider is occasional, not real-time.
CONFIG_POLL_FRAMES = 25

# How long to wait before retrying a failed RTSP connection
RECONNECT_DELAY_SECONDS = 5
MAX_RECONNECT_DELAY = 30  # Cap the backoff so we don't wait forever

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("camera-ingester")

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
_shutdown = False


def _handle_signal(signum, frame):
    """Handle SIGTERM/SIGINT for graceful Docker container shutdown."""
    global _shutdown
    logger.info("Shutdown signal received — finishing current frame...")
    _shutdown = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


# ---------------------------------------------------------------------------
# RTSP Connection
# ---------------------------------------------------------------------------
def connect_to_camera(rtsp_url: str) -> cv2.VideoCapture:
    """
    Open an RTSP stream via OpenCV.

    Uses TCP transport for reliability (UDP can drop frames on congested networks).
    Sets a small buffer size so we always get the most recent frame, not a stale
    buffered one — important for real-time security monitoring.
    """
    logger.info(f"Connecting to RTSP stream: {rtsp_url.split('@')[-1]}")  # Log URL without password

    # NOTE: OPENCV_FFMPEG_CAPTURE_OPTIONS is set at module top-level (before
    # `import cv2`) so the FFmpeg backend picks it up. Don't set it here.
    cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)

    # Small buffer = low latency (we want the latest frame, not queued ones)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    if not cap.isOpened():
        raise ConnectionError(f"Failed to open RTSP stream")

    # Read camera properties for logging
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    logger.info(f"Connected — resolution: {width}x{height}, camera FPS: {fps}")

    return cap


# ---------------------------------------------------------------------------
# Redis Connection
# ---------------------------------------------------------------------------
def connect_to_redis(host: str, port: int) -> redis.Redis:
    """
    Connect to Redis and verify the connection.

    The Redis client will auto-reconnect if the connection drops, but we
    verify it's reachable on startup to fail fast if Redis isn't running.
    """
    logger.info(f"Connecting to Redis at {host}:{port}")
    r = redis.Redis(host=host, port=port, decode_responses=False)
    r.ping()  # Raises ConnectionError if Redis is unreachable
    logger.info("Redis connection verified")
    return r


# ---------------------------------------------------------------------------
# Frame Publishing
# ---------------------------------------------------------------------------
def publish_frame(
    r: redis.Redis,
    frame: bytes,
    frame_number: int,
    width: int,
    height: int,
) -> None:
    """
    Publish a single JPEG-encoded frame to the Redis Stream.

    Uses XADD with MAXLEN to cap the stream size — old frames are automatically
    trimmed so Redis doesn't eat all your RAM. The '~' prefix on MAXLEN tells
    Redis to trim approximately (more efficient than exact trimming).

    The message fields match the FrameMessage schema in contracts/streams.py.
    """
    r.xadd(
        STREAM_KEY,
        {
            "camera_id": CAMERA_ID,
            "timestamp": str(time.time()),
            "frame": frame,
            "frame_number": str(frame_number),
            "width": str(width),
            "height": str(height),
        },
        maxlen=MAX_STREAM_LEN,
    )


# ---------------------------------------------------------------------------
# Main Ingestion Loop
# ---------------------------------------------------------------------------
def run():
    """
    Main loop: connect to camera + Redis, then continuously capture frames
    and publish them to the Redis Stream.

    Handles two failure modes:
    1. Camera disconnect — retries with exponential backoff
    2. Redis disconnect — Redis client auto-reconnects, we just retry the XADD
    """
    if not RTSP_URL:
        logger.error("RTSP_URL not set — check your .env or docker-compose.yml")
        sys.exit(1)

    logger.info(f"Starting camera ingester for '{CAMERA_ID}'")
    logger.info(f"Target FPS: {TARGET_FPS}, JPEG quality: {JPEG_QUALITY}")
    logger.info(f"Stream key: {STREAM_KEY}, max length: {MAX_STREAM_LEN}")

    # Connect to Redis (fail fast if it's not ready)
    r = connect_to_redis(REDIS_HOST, REDIS_PORT)

    frame_number = 0
    reconnect_delay = RECONNECT_DELAY_SECONDS
    current_fps = TARGET_FPS  # may be hot-reloaded from CONFIG_KEY below
    frame_interval = 1.0 / current_fps  # Seconds between frames

    while not _shutdown:
        # --- Connect to camera (with retry) ---
        try:
            cap = connect_to_camera(RTSP_URL)
            reconnect_delay = RECONNECT_DELAY_SECONDS  # Reset backoff on success
        except Exception as e:
            logger.error(f"Camera connection failed: {e}")
            logger.info(f"Retrying in {reconnect_delay}s...")
            time.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, MAX_RECONNECT_DELAY)
            continue

        # --- Read camera properties ---
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        # --- Frame capture loop ---
        last_frame_time = 0.0
        consecutive_failures = 0

        while not _shutdown:
            # Throttle to TARGET_FPS (don't flood Redis with 15 FPS if we only need 5)
            now = time.time()
            elapsed = now - last_frame_time
            if elapsed < frame_interval:
                # Read and discard frames to keep the buffer fresh
                cap.grab()
                time.sleep(0.001)  # Yield CPU
                continue

            # Read a frame from the RTSP stream
            ret, frame = cap.read()

            if not ret:
                consecutive_failures += 1
                if consecutive_failures >= 100:  # ~10s of failures before reconnect
                    logger.warning("Too many consecutive read failures — reconnecting...")
                    break  # Break inner loop to trigger reconnect
                time.sleep(0.1)
                continue

            consecutive_failures = 0

            # Encode frame as JPEG
            encode_params = [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]
            _, jpeg_buffer = cv2.imencode(".jpg", frame, encode_params)
            jpeg_bytes = jpeg_buffer.tobytes()

            # Publish to Redis Stream
            try:
                publish_frame(r, jpeg_bytes, frame_number, width, height)
            except redis.ConnectionError as e:
                logger.warning(f"Redis publish failed (will retry): {e}")
                time.sleep(1)
                continue

            frame_number += 1
            last_frame_time = time.time()

            # Hot-reload target_fps from Redis config so the dashboard slider
            # actually does something. Cheap HGET every CONFIG_POLL_FRAMES frames.
            if frame_number % CONFIG_POLL_FRAMES == 0:
                try:
                    raw = r.hget(CONFIG_KEY, b"target_fps")
                    if raw:
                        new_fps = int(float(raw.decode() if isinstance(raw, bytes) else raw))
                        if new_fps > 0 and new_fps != current_fps:
                            current_fps = new_fps
                            frame_interval = 1.0 / current_fps
                            logger.info(f"target_fps updated to {current_fps} (frame_interval={frame_interval:.3f}s)")
                except Exception:
                    pass  # Config read is best-effort

            # Log progress periodically (every 100 frames)
            if frame_number % 100 == 0:
                logger.info(
                    f"Published frame #{frame_number} "
                    f"({len(jpeg_bytes) / 1024:.1f} KB) @ {current_fps} FPS "
                    f"to {STREAM_KEY}"
                )

        # --- Cleanup after disconnect ---
        cap.release()
        if not _shutdown:
            logger.info(f"Camera disconnected — retrying in {reconnect_delay}s...")
            time.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, MAX_RECONNECT_DELAY)

    logger.info(f"Ingester stopped. Total frames published: {frame_number}")


# ---------------------------------------------------------------------------
# HD Stream Thread — reads RTSP main and caches latest frame in Redis
# ---------------------------------------------------------------------------
def run_hd_stream():
    """
    Background thread: reads the RTSP main stream (HD) and stores the
    latest frame in Redis as a simple key with a short TTL.
    The dashboard reads this key when the user switches to HD mode.
    """
    if not RTSP_MAIN_URL:
        logger.info("RTSP_MAIN not set — HD stream disabled")
        return

    logger.info(f"Starting HD stream thread for '{CAMERA_ID}'")
    r_hd = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=False)
    frame_interval = 1.0 / HD_TARGET_FPS
    reconnect_delay = 8  # Start higher than sub-stream to give it reconnect priority

    while not _shutdown:
        try:
            cap = connect_to_camera(RTSP_MAIN_URL)
            reconnect_delay = RECONNECT_DELAY_SECONDS
        except Exception as e:
            logger.warning(f"HD stream connection failed: {e}")
            time.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, MAX_RECONNECT_DELAY)
            continue

        last_frame_time = 0.0
        consecutive_failures = 0
        hd_frame_count = 0

        while not _shutdown:
            now = time.time()
            if now - last_frame_time < frame_interval:
                cap.grab()
                time.sleep(0.001)
                continue

            ret, frame = cap.read()
            if not ret:
                consecutive_failures += 1
                if consecutive_failures >= 100:  # ~10s of failures before reconnect
                    logger.warning("HD stream: too many failures — reconnecting")
                    break
                time.sleep(0.1)
                continue

            consecutive_failures = 0
            _, jpeg_buf = cv2.imencode(
                ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, HD_JPEG_QUALITY]
            )

            try:
                r_hd.setex(HD_FRAME_KEY, 5, jpeg_buf.tobytes())  # 5s TTL
            except redis.ConnectionError:
                time.sleep(1)
                continue

            hd_frame_count += 1
            last_frame_time = time.time()

            if hd_frame_count % 100 == 0:
                logger.info(f"HD stream: published frame #{hd_frame_count}")

        cap.release()
        if not _shutdown:
            # Stagger HD reconnect so it doesn't collide with sub-stream
            stagger = random.uniform(1, 5)
            time.sleep(reconnect_delay + stagger)
            reconnect_delay = min(reconnect_delay * 2, MAX_RECONNECT_DELAY)

    logger.info("HD stream thread stopped")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Start HD stream in a background thread
    if RTSP_MAIN_URL:
        hd_thread = threading.Thread(target=run_hd_stream, daemon=True)
        hd_thread.start()

    # Run the main (sub) stream ingester on the main thread
    run()
