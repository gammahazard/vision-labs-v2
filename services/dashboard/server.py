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
import logging

import cv2
import numpy as np
from contracts.redis_client import make_redis_client
from fastapi import FastAPI, Request
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
# Dashboard's "default" camera for legacy single-camera views (e.g. the old
# /single.html?camera=... has cam1 as its default). Multi-camera-aware code
# paths read the cameras:registry at request time and ignore this default.
CAMERA_ID = os.getenv("CAMERA_ID", "cam1")
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

# Default config values are owned by `routes/__init__.py` so server.py and
# the routers can't drift. Imported here for the startup seed below.
from routes import DEFAULT_CONFIG  # noqa: E402  (intentional late import)

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

# Redis connections — make_redis_client honors REDIS_PASSWORD when set
r = make_redis_client(decode_responses=True, host=REDIS_HOST, port=REDIS_PORT)       # text
r_bin = make_redis_client(decode_responses=False, host=REDIS_HOST, port=REDIS_PORT)  # binary (JPEG frames)

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
route_ctx.VEHICLE_DET_STREAM = VEHICLE_DET_STREAM
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
from routes.auth import (
    router as auth_router, init_auth_db, validate_session, session_must_change,
)
from routes.browse import router as browse_router
from routes.ai import router as ai_router, set_ai_db
from routes.telegram_access import router as telegram_access_router
from routes.metrics import router as metrics_router, start_metrics_collector
from routes.recordings import router as recordings_router
from routes.cameras import router as cameras_router
from routes.containers import router as containers_router
from routes.setup import router as setup_router, is_setup_complete, auto_mark_complete_if_preexisting

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
app.include_router(metrics_router)
app.include_router(recordings_router)
app.include_router(cameras_router)
app.include_router(containers_router)
app.include_router(setup_router)


# ---------------------------------------------------------------------------
# Auth Middleware — Protect all routes except login page and auth API
# ---------------------------------------------------------------------------
# Paths that don't require authentication
_AUTH_EXEMPT = {
    "/login.html", "/api/auth/login", "/api/auth/status",
    "/api/login-bg",
    "/css/style.css", "/js/core/auth.js", "/favicon.ico", "/favicon.svg",
    "/metrics",
}


# Paths the must-change-password gate lets through. The user must be able to
# load login.html (cancel button), call change-password, log out, see their
# current auth status, and load the styles/JS the login page needs.
_MUST_CHANGE_PASSWORD_ALLOWED = {
    "/login.html",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/status",
    "/api/auth/change-password",
    "/api/login-bg",
    "/css/style.css",
    "/js/core/auth.js",
    "/favicon.ico",
    "/favicon.svg",
}


_MUTATING_METHODS = {"POST", "PUT", "DELETE", "PATCH"}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Redirect unauthenticated requests to the login page."""
    path = request.url.path

    # CSRF / cross-origin defense (applies before auth so it also covers the
    # login + setup POSTs). For state-changing requests, if the browser sent an
    # Origin header it MUST match the Host we're served on. Same-origin
    # dashboard fetches always satisfy this; a cross-site forged POST carries
    # the attacker's Origin and is rejected. Absent Origin = a non-browser
    # client (curl/CLI) which isn't a CSRF vector, so it's allowed through.
    # Pairs with the SameSite=strict session cookie (routes/auth.py).
    if request.method in _MUTATING_METHODS:
        origin = request.headers.get("origin")
        if origin:
            from urllib.parse import urlparse
            origin_host = urlparse(origin).netloc
            if origin_host and origin_host != request.headers.get("host", ""):
                from fastapi.responses import JSONResponse
                return JSONResponse(
                    {"error": "cross-origin request refused"}, status_code=403,
                )

    # Allow exempt paths through
    if path in _AUTH_EXEMPT:
        return await call_next(request)

    # Check session cookie
    token = request.cookies.get("vl_session")
    username = validate_session(token)

    if not username:
        # Not authenticated — redirect browser requests, 401 for API
        if path.startswith("/api/") or path == "/ws":
            from fastapi.responses import JSONResponse
            return JSONResponse({"error": "Not authenticated"}, status_code=401)
        # 303 (See Other) forces browsers to GET the redirect target — safer
        # than the default 307 which can confuse browsers if the original was POST/etc.
        return RedirectResponse("/login.html", status_code=303)

    # Default-credentials gate: if the session is flagged "must change", refuse
    # every route except change-password (and the assets the login page needs
    # to render the rotation form). Prevents a CLI/curl client from bypassing
    # the frontend's UI-side enforcement by talking directly to the API.
    if session_must_change(token) and path not in _MUST_CHANGE_PASSWORD_ALLOWED:
        if path.startswith("/api/") or path == "/ws":
            from fastapi.responses import JSONResponse
            return JSONResponse(
                {"error": "Default credentials in use — change your password first.",
                 "must_change_password": True},
                status_code=403,
            )
        return RedirectResponse("/login.html?must_change=1", status_code=303)

    # First-run wizard gate: authenticated but setup hasn't completed.
    # The wizard endpoints + its static page must remain reachable; everything
    # else (other dashboard pages) redirects to /setup.html so the user can't
    # skip past it.
    if not _setup_exempt(path) and not is_setup_complete():
        # API calls from elsewhere get a 409 telling them setup is pending;
        # browser requests redirect to the wizard.
        if path.startswith("/api/") or path == "/ws":
            from fastapi.responses import JSONResponse
            return JSONResponse(
                {"error": "Setup pending — complete /setup.html first"},
                status_code=409,
            )
        return RedirectResponse("/setup.html", status_code=303)

    return await call_next(request)


# Paths that bypass the setup-gate (so the wizard itself + its assets work).
# Camera endpoints are listed explicitly — the wizard needs `discover`,
# `test-rtsp`, `onvif-stream-uri`, `next-slot`, plus POST/GET to register
# the first camera. We do NOT exempt a bare `/api/cameras` prefix because
# that would also leak the registry listing before setup completes.
_SETUP_GATE_EXEMPT_EXACT = {
    "/setup.html",
    "/js/pages/setup.js",
    "/css/setup.css",
    "/api/cameras",            # bare list/POST (next-slot upsert during wizard)
    "/api/cameras/next-slot",  # which slot the wizard should fill next
    "/api/cameras/discover",   # ONVIF scan
    "/api/cameras/test-rtsp",  # ffprobe check before saving
    "/api/cameras/onvif-stream-uri",  # SOAP to a discovered cam
}
_SETUP_GATE_EXEMPT_PREFIXES = ("/api/setup/", "/api/auth/", "/static/")


def _setup_exempt(path: str) -> bool:
    """The wizard needs cameras + auth endpoints to work, so allow those even
    while the setup-gate is active. Everything else is gated."""
    if path in _SETUP_GATE_EXEMPT_EXACT:
        return True
    return any(path.startswith(p) for p in _SETUP_GATE_EXEMPT_PREFIXES)


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

    # Backfill DEFAULT_CONFIG keys onto every existing per-camera config hash
    # using hsetnx — sets the key only if absent, so user-customized values
    # are never clobbered. This handles schema additions to DEFAULT_CONFIG
    # propagating to already-registered cameras (the bug: 2026-05-20 added
    # min_keypoints + kp_confidence_thresh to DEFAULT_CONFIG after /audit-repo
    # caught pose-detector reading them with no seed; existing cameras would
    # have continued falling back to pose-detector env defaults without this).
    try:
        camera_ids = list(r.hkeys("cameras:registry"))
        backfilled_count = 0
        for cam_id in camera_ids:
            cam_config_key = f"config:{cam_id}"
            for k, v in DEFAULT_CONFIG.items():
                if r.hsetnx(cam_config_key, k, v):
                    backfilled_count += 1
        if backfilled_count:
            logger.info(
                f"Backfilled {backfilled_count} missing DEFAULT_CONFIG key(s) "
                f"across {len(camera_ids)} camera config hash(es)"
            )
    except Exception as e:
        logger.warning(f"Per-camera DEFAULT_CONFIG backfill skipped: {e}")

    # Phase G: No more env-based camera seeding. Fresh installs start with
    # an EMPTY cameras:registry — the user adds their first camera via the
    # setup wizard (which auto-suggests cam1 as the slot ID). This is what
    # makes the user's first camera correctly go into the cam1 slot and
    # actually have services spawned for it. The old `seed_default_if_empty`
    # call here would auto-create a `front_door` entry from RTSP_SUB env,
    # which created a misleading "always-on primary" asymmetry.

    # First-run wizard gate: if we DIDN'T just create the camera registry
    # (i.e. this is a pre-existing install with cameras already in Redis),
    # mark setup as complete so existing users don't get force-marched
    # through the wizard after upgrading. Fresh installs leave setup.json
    # missing → the setup-gate middleware will redirect to /setup.html.
    auto_mark_complete_if_preexisting()

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

    # Daily prune of /data/snapshots and /data/events (configurable retention)
    from pollers.retention import retention_poller
    asyncio.create_task(retention_poller())

    # Disk + Redis-memory health alerts (Telegram broadcast when usage > 85%)
    from pollers.health import health_poller
    asyncio.create_task(health_poller())

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
# Mount AFTER API routes so /api/* takes priority.
# HTML files have no ?v= query string so browsers happily cache them
# indefinitely, which makes UI changes invisible until the user manually hard-
# refreshes. Set Cache-Control: no-cache on HTML responses so the browser
# always revalidates. Versioned assets (ai.js?v=N, conditions.js?v=N, etc.)
# stay cacheable as before — their URL changes when we bump the version.
from fastapi.staticfiles import StaticFiles as _StaticFiles


class NoCacheHtmlStaticFiles(_StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if isinstance(response, Response):
            if path.endswith(".html") or path in ("", "/"):
                response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


app.mount("/", NoCacheHtmlStaticFiles(directory="static", html=True), name="static")


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=DASHBOARD_PORT, log_level="info")
