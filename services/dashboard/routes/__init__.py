"""
routes/ — FastAPI APIRouter modules for the dashboard.

PURPOSE:
    Split server.py's REST API endpoints into focused modules.
    Each module creates a router via create_router() and receives
    shared dependencies (Redis client, stream keys, logger).

USAGE (in server.py):
    from routes.events import router as events_router
    app.include_router(events_router)

SHARED STATE:
    Each router module accesses the Redis client and key names
    from this package's module-level variables, set by server.py
    at startup before including routers.
"""

import redis
import logging

# Shared state — set by server.py before routers are included
r: redis.Redis = None           # Redis client (decode_responses=True — text)
r_bin: redis.Redis = None       # Redis client (decode_responses=False — binary/JPEG)
logger: logging.Logger = None   # Logger instance
FACE_API_URL: str = ""          # face-recognizer service URL

# Redis key names — set by server.py
EVENT_STREAM: str = ""
FRAME_STREAM: str = ""
DETECTION_STREAM: str = ""
STATE_KEY: str = ""
CONFIG_KEY: str = ""

ZONE_KEY: str = ""
AUTH_DB_PATH: str = ""
VEHICLE_SNAPSHOT_DIR: str = ""       # Vehicle snapshot disk storage root
CAMERA_ID: str = "front_door"        # Camera identifier (set by server.py)
HD_FRAME_KEY: str = ""               # HD frame Redis key (set by server.py)
IDENTITY_KEY: str = ""               # Identity state Redis key (set by server.py)
TELEGRAM_USERS_KEY: str = ""         # Telegram authorized users hash (set by server.py)
TELEGRAM_ACCESS_LOG: str = ""        # Telegram access log stream (set by server.py)

# Default config values (must match server.py DEFAULT_CONFIG)
DEFAULT_CONFIG = {
    "confidence_thresh": "0.5",
    "iou_threshold": "0.3",
    "lost_timeout": "5.0",
    "target_fps": "5",
    "notify_person": "1",
    "notify_vehicle": "1",
    "suppress_known": "0",
    "notify_cooldown": "60",
    "vehicle_cooldown": "60",
    "vehicle_confidence_thresh": "0.35",
    "vehicle_idle_timeout": "90",
}
