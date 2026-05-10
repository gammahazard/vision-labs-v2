"""
services/dashboard/server.py — FastAPI backend for the Vision Labs dashboard.

PURPOSE:
    Serves the web dashboard and provides real-time data to the browser:
    - WebSocket streaming of live camera frames with detection data
    - REST API routes (modularized into routes/ package)

RELATIONSHIPS:
    - Reads from: Redis streams (frames, detections, events, state)
    - Writes to: Redis config key (when user adjusts settings)
    - Serves: static frontend files (index.html, style.css, *.js)
    - Used by: browser at http://localhost:8080

DATA FLOW:
    Redis → THIS SERVICE (WebSocket) → Browser (renders frames + overlays)
    Browser (settings change) → THIS SERVICE (REST) → Redis config key → Detector reads it

MODULES:
    routes/events.py      — GET /api/events
    routes/config.py      — GET/POST /api/config, GET /api/stats
    routes/conditions.py  — GET /api/conditions (time + weather)
    routes/faces.py       — Face enrollment proxies (5 endpoints)
    routes/unknowns.py    — Unknown face proxies (5 endpoints)
    routes/zones.py       — Zone CRUD (3 endpoints)
"""

import asyncio
import os
import time
import logging

import cv2
import numpy as np
import redis
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse, Response

# Import stream key definitions from contracts (single source of truth)
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "contracts"))
from streams import (
    FRAME_STREAM as _FRAME_TMPL,
    DETECTION_STREAM as _DET_TMPL,
    EVENT_STREAM as _EVT_TMPL,
    STATE_KEY as _STATE_TMPL,
    CONFIG_KEY as _CFG_TMPL,
    IDENTITY_KEY as _IDKEY_TMPL,
    ZONE_KEY as _ZONE_TMPL,
    HD_FRAME_KEY as _HD_TMPL,
    VEHICLE_STREAM as _VEH_DET_TMPL,
    DETECTION_FRAME_KEY as _DET_FRAME_TMPL,
    TELEGRAM_USERS_KEY as _TG_USERS_KEY,
    TELEGRAM_ACCESS_LOG as _TG_ACCESS_LOG,
    stream_key,
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CAMERA_ID = os.getenv("CAMERA_ID", "front_door")
REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8080"))
FACE_API_URL = os.getenv("FACE_API_URL", "http://127.0.0.1:8081")

# Redis keys — resolved from contracts/streams.py
FRAME_STREAM = stream_key(_FRAME_TMPL, camera_id=CAMERA_ID)
DETECTION_STREAM = stream_key(_DET_TMPL, detector_type="pose", camera_id=CAMERA_ID)
EVENT_STREAM = stream_key(_EVT_TMPL, camera_id=CAMERA_ID)
STATE_KEY = stream_key(_STATE_TMPL, camera_id=CAMERA_ID)
CONFIG_KEY = stream_key(_CFG_TMPL, camera_id=CAMERA_ID)
IDENTITY_KEY = stream_key(_IDKEY_TMPL, camera_id=CAMERA_ID)
ZONE_KEY = stream_key(_ZONE_TMPL, camera_id=CAMERA_ID)
HD_FRAME_KEY = stream_key(_HD_TMPL, camera_id=CAMERA_ID)
VEHICLE_DET_STREAM = stream_key(_VEH_DET_TMPL, camera_id=CAMERA_ID)
DETECTION_FRAME_POSE = stream_key(_DET_FRAME_TMPL, detector_type="pose", camera_id=CAMERA_ID)

# Default config values (written to Redis on first startup if not present)
DEFAULT_CONFIG = {
    "confidence_thresh": "0.5",
    "iou_threshold": "0.3",
    "lost_timeout": "5.0",
    "target_fps": "10",
    # Notification preferences (Phase 6.5)
    "notify_person": "1",          # Send Telegram alerts for person detections
    "notify_vehicle": "1",         # Send Telegram alerts for vehicle events
    "suppress_known": "0",         # Auto-suppress alerts for known/identified people
    "notify_cooldown": "60",       # Seconds between person notifications
    "vehicle_cooldown": "60",      # Seconds between vehicle notifications
    "vehicle_confidence_thresh": "0.35",  # Vehicle detector YOLO confidence
    "vehicle_idle_timeout": "90",  # Seconds before vehicle_idle alert
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("dashboard")

# Silence httpx — it logs every outbound HTTP request URL at INFO level,
# which leaks the Telegram bot token (https://api.telegram.org/bot<TOKEN>/...)
# into log output. Anyone who sees the logs has the bot. Only WARNING+.
logging.getLogger("httpx").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# App Setup
# ---------------------------------------------------------------------------
app = FastAPI(title="Vision Labs Dashboard")

# Redis connections
r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)       # text
r_bin = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=False)   # binary (JPEG frames)

# Auth database path (Docker volume for persistence)
AUTH_DB_PATH = os.getenv("AUTH_DB_PATH", "/data/auth.db")


# ---------------------------------------------------------------------------
# Inject shared state into routes package, then include routers
# ---------------------------------------------------------------------------
import routes as route_ctx

route_ctx.r = r
route_ctx.r_bin = r_bin
route_ctx.logger = logger
route_ctx.FACE_API_URL = FACE_API_URL
route_ctx.EVENT_STREAM = EVENT_STREAM
route_ctx.FRAME_STREAM = FRAME_STREAM
route_ctx.DETECTION_STREAM = DETECTION_STREAM
route_ctx.STATE_KEY = STATE_KEY
route_ctx.CONFIG_KEY = CONFIG_KEY
route_ctx.IDENTITY_KEY = IDENTITY_KEY
route_ctx.ZONE_KEY = ZONE_KEY
route_ctx.AUTH_DB_PATH = AUTH_DB_PATH

# Vehicle snapshot disk storage (day-organized)
VEHICLE_SNAPSHOT_DIR = os.path.join(os.environ.get("SNAPSHOT_DIR", "/data/snapshots"), "vehicles")
os.makedirs(VEHICLE_SNAPSHOT_DIR, exist_ok=True)
route_ctx.VEHICLE_SNAPSHOT_DIR = VEHICLE_SNAPSHOT_DIR
route_ctx.CAMERA_ID = CAMERA_ID
route_ctx.HD_FRAME_KEY = HD_FRAME_KEY
route_ctx.VEHICLE_DET_STREAM = VEHICLE_DET_STREAM
route_ctx.DETECTION_FRAME_POSE = DETECTION_FRAME_POSE
route_ctx.TELEGRAM_USERS_KEY = _TG_USERS_KEY
route_ctx.TELEGRAM_ACCESS_LOG = _TG_ACCESS_LOG

from routes.events import router as events_router
from routes.config import router as config_router
from routes.conditions import router as conditions_router
from routes.faces import router as faces_router
from routes.unknowns import router as unknowns_router
from routes.zones import router as zones_router
from routes.notifications import router as notifications_router
from routes.auth import router as auth_router, init_auth_db, validate_session
from routes.browse import router as browse_router
from routes.ai import router as ai_router, set_ai_db, set_gpu_ready_flag
from routes.telegram_access import router as telegram_access_router
from routes.image_gen import router as image_gen_router
from routes.metrics import router as metrics_router, start_metrics_collector
from routes.recordings import router as recordings_router
from routes.cameras import router as cameras_router

app.include_router(events_router)
app.include_router(config_router)
app.include_router(conditions_router)
app.include_router(faces_router)
app.include_router(unknowns_router)
app.include_router(zones_router)
app.include_router(notifications_router)
app.include_router(auth_router)
app.include_router(browse_router)
app.include_router(ai_router)
app.include_router(telegram_access_router)
app.include_router(image_gen_router)
app.include_router(metrics_router)
app.include_router(recordings_router)
app.include_router(cameras_router)


# ---------------------------------------------------------------------------
# Auth Middleware — Protect all routes except login page and auth API
# ---------------------------------------------------------------------------
# Paths that don't require authentication
_AUTH_EXEMPT = {
    "/login.html", "/api/auth/login", "/api/auth/status",
    "/api/login-bg",
    "/style.css", "/auth.js", "/favicon.ico",
    "/metrics",
}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Redirect unauthenticated requests to the login page."""
    path = request.url.path

    # Allow exempt paths through
    if path in _AUTH_EXEMPT:
        return await call_next(request)

    # Check session cookie
    token = request.cookies.get("vl_session")
    username = validate_session(token)

    if username:
        return await call_next(request)

    # Not authenticated — redirect browser requests, 401 for API
    if path.startswith("/api/") or path == "/ws":
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    return RedirectResponse("/login.html")


# ---------------------------------------------------------------------------
# Public login background — heavily blurred camera snapshot (no auth)
# ---------------------------------------------------------------------------
@app.get("/api/login-bg")
async def login_background():
    """Serve a small, heavily blurred snapshot for the login page background.
    No authentication required, but the image is blurred beyond recognition
    so it cannot be used for surveillance."""
    try:
        frame = None
        # Try HD frame first
        if route_ctx.HD_FRAME_KEY:
            frame = route_ctx.r_bin.get(route_ctx.HD_FRAME_KEY.encode())
        # Fall back to sub-stream
        if not frame:
            entries = route_ctx.r_bin.xrevrange(route_ctx.FRAME_STREAM.encode(), count=1)
            if entries:
                _, data = entries[0]
                frame = data.get(b"frame")
        if not frame:
            return Response(status_code=204)
        # Decode, blur heavily, shrink, and re-encode at low quality
        np_arr = np.frombuffer(frame, np.uint8)
        img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        if img is None:
            return Response(status_code=204)
        # Resize to small (fast blur), then blur aggressively
        h, w = img.shape[:2]
        small = cv2.resize(img, (w // 4, h // 4))
        blurred = cv2.GaussianBlur(small, (51, 51), 30)
        _, jpeg = cv2.imencode(".jpg", blurred, [cv2.IMWRITE_JPEG_QUALITY, 30])
        return Response(content=jpeg.tobytes(), media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})
    except Exception as exc:
        logger.warning("login-bg failed: %s", exc, exc_info=True)
        return Response(status_code=204)


# ---------------------------------------------------------------------------
# Startup — Initialize default config if not set
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def startup():
    """Initialize auth DB, write default config to Redis, start background tasks."""
    # Initialize auth database (creates default admin/admin if empty)
    init_auth_db()
    logger.info("Auth database initialized")

    existing = r.hgetall(CONFIG_KEY)
    if not existing:
        r.hset(CONFIG_KEY, mapping=DEFAULT_CONFIG)
        logger.info(f"Initialized default config in {CONFIG_KEY}")
    else:
        logger.info(f"Config already exists in {CONFIG_KEY}: {existing}")

    # Seed the camera registry with this deployment's single env-configured
    # camera so the API has at least one entry. On subsequent boots the
    # registry is non-empty and this is a no-op.
    import cameras as camera_registry
    rtsp_sub = os.getenv("RTSP_SUB", "")
    rtsp_main = os.getenv("RTSP_MAIN", "")
    camera_registry.seed_default_if_empty(
        default_id=CAMERA_ID,
        default_name=os.getenv("LOCATION_NAME", CAMERA_ID.replace("_", " ").title()),
        rtsp_sub=rtsp_sub,
        rtsp_main=rtsp_main,
        location_lat=float(os.getenv("LOCATION_LAT", "0") or "0"),
        location_lon=float(os.getenv("LOCATION_LON", "0") or "0"),
    )

    # Initialize AI assistant database
    from ai_db import AIDB
    global _ai_db
    _ai_db = AIDB("/data/ai.db")
    set_ai_db(_ai_db)
    logger.info("AI assistant database initialized")

    # Start background event notification poller
    from pollers.events import event_notification_poller
    asyncio.create_task(event_notification_poller())

    # Start Telegram callback poller (receives commands)
    from routes.bot_commands import poll_telegram_callbacks
    asyncio.create_task(poll_telegram_callbacks())

    # Start reminder poller (checks every 60s for due reminders)
    from pollers.reminders import reminder_poller
    asyncio.create_task(reminder_poller(_ai_db))

    # Pull the AI model on first startup (background)
    # Pass a callback so the warm-up can signal when the model is in GPU memory
    from pollers.ollama_warmup import warm_ollama
    asyncio.create_task(warm_ollama())

    # Clear stale ComfyUI queue and GPU pause flag from previous session
    from pollers.comfyui_cleanup import clear_comfyui_queue
    asyncio.create_task(clear_comfyui_queue())

    # Daily prune of /data/snapshots and /data/events (configurable retention)
    from pollers.retention import retention_poller
    asyncio.create_task(retention_poller())

    # Start Prometheus metrics collector (polls Redis every 10s)
    asyncio.create_task(start_metrics_collector())

    logger.info(f"Dashboard ready at http://localhost:{DASHBOARD_PORT}")


# ---------------------------------------------------------------------------
# WebSocket — /ws/live (extracted to websocket.py for clarity)
# ---------------------------------------------------------------------------
from websocket import register as register_websocket
register_websocket(app)


# ---------------------------------------------------------------------------
# Static Files — Serve frontend
# ---------------------------------------------------------------------------
# Mount AFTER API routes so /api/* takes priority
app.mount("/", StaticFiles(directory="static", html=True), name="static")


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=DASHBOARD_PORT, log_level="info")
