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
    VEHICLE_SAMPLE_EVENT,  # noqa: F401  (re-exported)
    VEHICLE_GONE_EVENT,    # noqa: F401  (re-exported)
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
# Bumped 5.0 → 30.0 so a parked car briefly occluded by drive-by traffic
# (delivery van, garbage truck stopping in front for >15 s) gets re-attached
# to the same TrackedVehicle on the other side of the disturbance, instead of
# the ghost expiring → vehicle_left → fresh track → fresh vehicle_idle (and
# the position dedup at the notify layer would need to do all the work). With
# the bump, the effective occlusion grace is LOST_TIMEOUT (10s) + GHOST_TTL
# (30s) = 40 s — wider than any realistic drive-by but still tight enough
# that a genuinely-departed car fires vehicle_left within ~45 s of leaving.
VEHICLE_GHOST_TTL = float(os.getenv("VEHICLE_GHOST_TTL", "30.0"))
# Idle-confirmed vehicles (TrackedVehicle.idle_alerted == True) get a MUCH
# longer ghost window — a parked car detected intermittently every 10-15
# minutes (RTSP reconnect, frame_hd TTL hiccup) would otherwise spawn a
# new track every gap. Default 600 s = 10 min covers realistic detector
# stutter without making the track ID stale across actual departures.
VEHICLE_IDLE_GHOST_TTL = float(os.getenv("VEHICLE_IDLE_GHOST_TTL", "600.0"))
# Center-distance threshold expressed as a multiple of the bbox width.
# Used both by `_try_ghost_match` (re-associating recently-departed tracks
# from the ghost buffer) and `_try_live_center_match` (catching the IoU
# identity-swap on fast-moving cars across consecutive detection frames).
#
# Bumped 2.0 → 3.5 after live cam1 data showed a single car shifting 225 px
# between detections 1.1 s apart on a wide-angle fish-eye lens. The old
# threshold (bbox_w * 2.0 ≈ 176 px for an 88-px-wide bbox) was too tight;
# the new threshold (~308 px) covers a typical drive-by while still being
# narrower than the distance between two cars passing in different lanes
# of the same frame (which is usually 400+ px on a residential-street view).
VEHICLE_GHOST_MAX_DIST_RATIO = float(os.getenv("VEHICLE_GHOST_MAX_DIST_RATIO", "3.5"))
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
