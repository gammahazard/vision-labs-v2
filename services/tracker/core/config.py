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
# Non-idle tracks (moving vehicles) should age out of tracked_vehicles
# much faster than idle/parked ones. A fast-moving car takes 1-2 s to
# traverse this camera's FOV; after 3 s of no detection it's gone, full
# stop. Keeping it in tracked_vehicles for the historical 10 s is what
# let a school bus inherit a small car's track 18 s later (live, 15:09)
# and a pickup track absorb a red SUV's spawn frame (live, 15:36).
# Idle/stationary tracks still use the longer VEHICLE_LOST_TIMEOUT
# (handles detector stutter on permanently-parked vehicles).
VEHICLE_LOST_TIMEOUT_DRIVING = float(os.getenv("VEHICLE_LOST_TIMEOUT_DRIVING", "3.0"))
# `_try_live_center_match` (the loose center-distance fallback for IoU
# misses) uses bbox_w * VEHICLE_GHOST_MAX_DIST_RATIO as its radius —
# fine for consecutive-frame jitter (200 ms drift), wrong for stale
# tracks. After VEHICLE_CENTER_MATCH_STALE_SECS of no match, skip the
# live-center fallback entirely. Forces stale tracks to lose detections
# they can't legitimately claim, while preserving the fast-mover rescue
# for which the fallback was designed.
VEHICLE_CENTER_MATCH_STALE_SECS = float(os.getenv("VEHICLE_CENTER_MATCH_STALE_SECS", "2.0"))
# Minimum bbox area (in sub-stream pixels, 896×512) for a detection to
# be sampled into the va buffer. Sub-threshold bboxes are usually:
# (a) the vehicle entering frame-edge with only its bumper visible
# (b) the vehicle leaving frame-edge with only a sliver remaining
# In both cases the crop is mostly road/background, polluting the
# classifier's color/body vote. The MIN_CROP_AREA_HD_PX gate in the va
# service already drops these downstream, but emitting them costs a
# Redis HD-frame write + a stream XADD per skip. Filter at the source.
# 1500 px² ≈ 40×38 sub-stream → 100×95 HD with default scaling.
MIN_SAMPLE_BBOX_AREA_SUB_PX = int(os.getenv("MIN_SAMPLE_BBOX_AREA_SUB_PX", "1500"))
VEHICLE_IOU_THRESHOLD = float(os.getenv("VEHICLE_IOU_THRESHOLD", "0.2"))
# Idle-confirmed tracks demand a much tighter IoU before accepting a new
# detection. A parked car's bbox is fixed, so a real re-detection of the
# same car has near-perfect overlap (≥0.7 typical). A drive-by car
# passing through the idle bbox would IoU around 0.2–0.4 and used to be
# merged in — its crops then ended up in the parked car's track buffer,
# polluting the classifier vote (observed live: vehicle_0001 angle_5
# was a different physical vehicle).
VEHICLE_IDLE_IOU_THRESHOLD = float(os.getenv("VEHICLE_IDLE_IOU_THRESHOLD", "0.65"))
# Escape hatch for the tight idle IoU gate above. YOLO sometimes outputs
# a slightly-wider/taller bbox on a parked car (curb shadow, adjacent
# vehicle clipping), and that jittered detection has IoU below 0.65 but
# intersection-over-min ≈ 1.0 (the smaller box is fully inside the
# larger). Without an escape hatch the jittered detection spawned a
# phantom track on top of the parked car and fired a duplicate
# vehicle_idle 150s later. Observed live: vehicle_0001 [w=75,h=35] vs
# incoming [w=109,h=44], IoU=0.53, IoM=0.99 — duplicate idle at 12:25.
# Area-ratio gate (≤ VEHICLE_IDLE_IOM_AREA_RATIO_MAX) prevents a person
# or small object inside a parked-truck bbox from false-merging.
VEHICLE_IDLE_IOM_THRESHOLD = float(os.getenv("VEHICLE_IDLE_IOM_THRESHOLD", "0.9"))
VEHICLE_IDLE_IOM_AREA_RATIO_MAX = float(os.getenv("VEHICLE_IDLE_IOM_AREA_RATIO_MAX", "2.0"))
# Size-ratio sanity gate for primary IoU matches against STALE tracks
# (last_seen > VEHICLE_MATCH_STALE_SECS ago). A non-idle track that
# hasn't been refreshed in >1 s sitting at e.g. [80x40] should not be
# allowed to claim a new detection at [180x110] just because their
# IoU is above 0.2 — they're almost certainly two different physical
# vehicles entering the same screen region. Live regression: a small
# car briefly detected at 15:09:45, then 10 s later a bus drove
# through the same general area; the bus's wider bbox got merged
# into the car's track via primary IoU 0.33, polluting the classifier
# vote ("yellow SUV AM General" for what should have been a school
# bus). Recent matches (≤ 1 s gap) skip the gate so legitimate
# frame-to-frame bbox jitter still works.
VEHICLE_MATCH_STALE_SECS = float(os.getenv("VEHICLE_MATCH_STALE_SECS", "1.0"))
VEHICLE_MATCH_AREA_RATIO_MAX = float(os.getenv("VEHICLE_MATCH_AREA_RATIO_MAX", "2.5"))
# Skip `vehicle_sample` emit when another currently-tracked non-idle
# (i.e., moving) vehicle's bbox overlaps this track's bbox by more than
# this IoU. Motivating case: a parked car's bbox region in the HD frame
# captures pixels of a passing vehicle when the passing vehicle
# physically occludes (or sits in front of) the parked car. Without the
# skip, the parked car's classifier buffer accumulates crops with two
# vehicles visible — body/make/model votes get pulled toward whatever
# the foreground intruder looks like. The reservoir still fills from
# unoccluded frames before/after the drive-by; skipping a few contested
# samples doesn't starve a long parked-car track. Idle-vs-idle overlaps
# (two parked cars adjacent) are NOT skipped because both vehicles
# remain in the same relative position — no transient pollution.
SAMPLE_OCCLUSION_IOU_THRESHOLD = float(os.getenv("SAMPLE_OCCLUSION_IOU_THRESHOLD", "0.15"))
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
# Once an action is committed, this multiplier is applied to the
# debounce to require MORE consecutive frames before flipping to a
# new action. Bumped 1 → 2 so a brief pose oscillation (gait swing,
# leaning over, half-second partial occlusion) doesn't immediately
# flip the committed action. 1 = no extra stickiness, 2 = needs 2×
# evidence to flip, 3 = 3×, etc.
ACTION_STICKY_MULTIPLIER = int(os.getenv("ACTION_STICKY_MULTIPLIER", "2"))
# Minimum visible keypoints (conf ≥ KP_CONFIDENCE_THRESH in the
# pose-detector) before we attempt action classification. Below this,
# the action is set to "unknown" — partial detections (only shoulders
# visible, lower body occluded) produced unreliable arms_raised /
# crouching labels. 17 is the COCO total; 10 = "we can see at least
# the upper body + most of the lower body."
MIN_KEYPOINTS_FOR_ACTION = int(os.getenv("MIN_KEYPOINTS_FOR_ACTION", "10"))
MIN_BBOX_AREA = int(os.getenv("MIN_BBOX_AREA", "3072"))
IDENTITY_GRACE_SECONDS = float(os.getenv("IDENTITY_GRACE_SECONDS", "4.0"))
# How often the tracker polls identity_state:{cam} to see what the
# face-recognizer matched. The dashboard's live-video overlay polls at
# 10 fps and turns bboxes blue + writes names within ~100 ms of a
# match — but the tracker is what fires the `person_identified` event
# (which hits the Telegram alert + recent-activity feed). If a face is
# visible only briefly (a person walking past, head turned for a
# moment), a 2 s poll easily lands BEFORE the match and again AFTER,
# missing the identification entirely. 0.5 s catches anything that
# lingers ≥ 500 ms, which is the typical face-visible window for a
# walking pedestrian. Cost: ≈2 extra HGETALL/sec/cam — negligible.
IDENTITY_POLL_INTERVAL = float(os.getenv("IDENTITY_POLL_INTERVAL", "0.5"))

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
