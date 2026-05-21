"""
tracker/core/config.py — environment variables + stream keys + logger.

Single source of truth for tracker tuning constants. Re-exported by sibling
modules. Keeping these in one place means a future env-knob promotion (e.g.
dashboard slider for IOU_THRESHOLD) only changes one file.
"""

import os
import logging

# contracts/ is mounted at /app/contracts in the tracker container (see
# docker-compose.yml volumes for tracker-camN). It's a real package with
# __init__.py, so we import via dotted form rather than path-mangling.
from contracts.actions import classify_action  # noqa: F401  (re-exported)
from contracts.time_rules import point_in_polygon, should_alert, get_time_period  # noqa: F401
from contracts.streams import (
    DETECTION_STREAM as _DET_TMPL,
    EVENT_STREAM as _EVT_TMPL,
    STATE_KEY as _STATE_TMPL,
    CONFIG_KEY as _CFG_TMPL,
    ZONE_KEY as _ZONE_TMPL,
    IDENTITY_KEY as _IDKEY_TMPL,
    VEHICLE_STREAM as _VEH_TMPL,
    VEHICLE_SNAPSHOT_KEY as _VSNAP_TMPL,
    VEHICLE_SNAPSHOT_BBOX_KEY as _VSNAP_BBOX_TMPL,
    PERSON_SNAPSHOT_KEY as _PSNAP_TMPL,
    FRAME_STREAM as _FRAME_TMPL,
    HD_FRAME_KEY as _HD_FRAME_TMPL,
    stream_key,
)

CAMERA_ID = os.getenv("CAMERA_ID", "cam1")
REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
IOU_THRESHOLD = float(os.getenv("IOU_THRESHOLD", "0.3"))
LOST_TIMEOUT = float(os.getenv("LOST_TIMEOUT", "8.0"))
CONSUMER_GROUP = os.getenv("CONSUMER_GROUP", "trackers")
VEHICLE_CONSUMER_GROUP = os.getenv("VEHICLE_CONSUMER_GROUP", "vehicle_trackers")
CONSUMER_NAME = os.getenv("CONSUMER_NAME", "tracker_1")

# Stream keys — resolved from contracts/streams.py
DETECTION_STREAM = stream_key(_DET_TMPL, detector_type="pose", camera_id=CAMERA_ID)
EVENT_STREAM = stream_key(_EVT_TMPL, camera_id=CAMERA_ID)
STATE_KEY = stream_key(_STATE_TMPL, camera_id=CAMERA_ID)
VEHICLE_STREAM = stream_key(_VEH_TMPL, camera_id=CAMERA_ID)
CONFIG_KEY = stream_key(_CFG_TMPL, camera_id=CAMERA_ID)
ZONE_KEY = stream_key(_ZONE_TMPL, camera_id=CAMERA_ID)
IDENTITY_KEY = stream_key(_IDKEY_TMPL, camera_id=CAMERA_ID)
FRAME_STREAM = stream_key(_FRAME_TMPL, camera_id=CAMERA_ID)
HD_FRAME_KEY = stream_key(_HD_FRAME_TMPL, camera_id=CAMERA_ID)

# Tuning constants — env-overridable so the dashboard can promote any of
# these to a UI knob later without a rebuild. Defaults preserve historic
# behavior verbatim.
MAX_EVENT_STREAM_LEN = int(os.getenv("MAX_EVENT_STREAM_LEN", "5000"))
VEHICLE_IDLE_TIMEOUT = float(os.getenv("VEHICLE_IDLE_TIMEOUT", "90.0"))
VEHICLE_LOST_TIMEOUT = float(os.getenv("VEHICLE_LOST_TIMEOUT", "10.0"))
VEHICLE_IOU_THRESHOLD = float(os.getenv("VEHICLE_IOU_THRESHOLD", "0.2"))
VEHICLE_GHOST_TTL = float(os.getenv("VEHICLE_GHOST_TTL", "5.0"))
VEHICLE_GHOST_MAX_DIST_RATIO = float(os.getenv("VEHICLE_GHOST_MAX_DIST_RATIO", "2.0"))
CONFIG_RELOAD_INTERVAL = int(os.getenv("CONFIG_RELOAD_INTERVAL", "10"))
ACTION_DEBOUNCE_FRAMES = int(os.getenv("ACTION_DEBOUNCE_FRAMES", "10"))
ACTION_STICKY_MULTIPLIER = int(os.getenv("ACTION_STICKY_MULTIPLIER", "1"))
MIN_BBOX_AREA = int(os.getenv("MIN_BBOX_AREA", "3072"))
IDENTITY_GRACE_SECONDS = float(os.getenv("IDENTITY_GRACE_SECONDS", "4.0"))

# Re-exported snapshot key templates. The underscore aliases preserve the
# names the legacy monolithic tracker.py used internally so manager.py can
# import them under either name.
VEHICLE_SNAPSHOT_KEY_TMPL = _VSNAP_TMPL
VEHICLE_SNAPSHOT_BBOX_KEY_TMPL = _VSNAP_BBOX_TMPL
PERSON_SNAPSHOT_KEY_TMPL = _PSNAP_TMPL

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("tracker")
