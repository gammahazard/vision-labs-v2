"""tracker/core/manager.py — PersonTracker orchestrator class.

Owns the dictionaries of currently-tracked people + vehicles, runs the
IoU matching, and keeps the camera state hash in sync.

Split into mixins on 2026-05-22 — the per-area methods now live next to
each other:
    _vehicle_matcher.py  — fallback match strategies (idle-IoM, ghost, center-distance)
    _vehicle_events.py   — vehicle_detected / sample / idle / left / gone emit
    _person_events.py    — person event emit + snapshot pairing
    _zones.py            — zone polygon load + dead-zone gate
    _identity.py         — face-recognizer → track identity sync
    _classes.py          — YOLO car↔truck↔bus equivalence helper

What remains here:
    __init__, _read_face_recognition_flag, _generate_id
    _process_vehicle_detections — the vehicle-pipeline orchestrator
        (kept here so the env-driven sample-throttle constants below
         stay reloadable via `importlib.reload(manager)` — many tests
         monkeypatch then reload to flip the sample-emit policy)
    _update_state, update — the person-pipeline orchestrator
"""

import json
import os
import time

import redis

from .config import (
    logger,
    CAMERA_ID,
    STATE_KEY,
    VEHICLE_SNAPSHOT_KEY_TMPL as _VSNAP_TMPL,
    VEHICLE_SNAPSHOT_BBOX_KEY_TMPL as _VSNAP_BBOX_TMPL,
    VEHICLE_IDLE_TIMEOUT,
    VEHICLE_LOST_TIMEOUT,
    VEHICLE_LOST_TIMEOUT_DRIVING,
    MIN_SAMPLE_BBOX_AREA_SUB_PX,
    VEHICLE_IOU_THRESHOLD,
    VEHICLE_IDLE_IOU_THRESHOLD,
    VEHICLE_MATCH_STALE_SECS,
    VEHICLE_MATCH_AREA_RATIO_MAX,
    VEHICLE_GHOST_TTL,
    VEHICLE_IDLE_GHOST_TTL,
    MIN_BBOX_AREA,
    IDENTITY_GRACE_SECONDS,
    IDENTITY_TRACK_IOU_THRESHOLD,
    IDENTITY_LOST_TIMEOUT,
    IDENTITY_PERSIST_GAP_SECS,
    stream_key,
)
from .iou import compute_iou
from .state import TrackedPerson, TrackedVehicle
from ._identity import IdentityMixin
from ._person_events import PersonEventsMixin
from ._vehicle_events import VehicleEventsMixin
from ._vehicle_matcher import VehicleMatcherMixin
from ._zones import ZonesMixin

# Phase 1 of the vehicle-attributes pipeline. Off by default until the
# consumer (vehicle-attributes-cam{N}) is wired up. See spec §2.2.
# These are module-level so importlib.reload(manager) re-reads them when
# tests monkeypatch os.environ before reloading.
EMIT_VEHICLE_SAMPLES = os.getenv("EMIT_VEHICLE_SAMPLES", "0") == "1"
SAMPLE_INTERVAL_FRAMES = max(1, int(os.getenv("SAMPLE_INTERVAL_FRAMES", "3")))
# Emit a sample on EVERY matched update for the first N frames of a new
# track. After this window we fall back to the SAMPLE_INTERVAL_FRAMES
# throttle. Reason: brief drive-bys produce only a handful of detector
# hits (observed: 6 hits across two tracks for a single 2.5s truck pass).
# With SAMPLE_INTERVAL_FRAMES=3 + spawn-not-sampled, those 6 hits became
# 1 stored crop. EAGER_SAMPLE_FRAMES default matches buffer.max_crops in
# vehicle-attributes (8) — if the detector sees the vehicle 8 times we
# fill the reservoir exactly; longer tracks fall back to the throttle so
# parked-car traffic doesn't 3x the Redis HD-sample writes.
EAGER_SAMPLE_FRAMES = max(0, int(os.getenv("EAGER_SAMPLE_FRAMES", "8")))


class PersonTracker(
    VehicleMatcherMixin,
    VehicleEventsMixin,
    PersonEventsMixin,
    ZonesMixin,
    IdentityMixin,
):
    """
    Tracks people across frames using IoU matching.

    Maintains a dictionary of currently tracked people. On each new set of
    detections, matches them to existing tracks or creates new ones.
    Emits events when people appear or leave.

    Inherits domain methods from five mixins — see this module's docstring
    for the layout. None of the mixins override each other; MRO order is
    purely for documentation grouping.
    """

    def __init__(self, r: redis.Redis, iou_threshold: float = VEHICLE_IOU_THRESHOLD,
                 lost_timeout: float = VEHICLE_LOST_TIMEOUT):
        self.r = r
        self.iou_threshold = iou_threshold
        self.lost_timeout = lost_timeout
        self.tracked: dict[str, TrackedPerson] = {}  # person_id → TrackedPerson
        self.next_id = 1  # Simple incrementing ID counter
        self.total_events = 0
        self._zones = {}         # zone_id → zone data
        self._zone_load_time = 0  # Timestamp of last zone load
        self._zone_reload_interval = 10  # Reload zones every N seconds
        self.frame_width = 640   # Updated from detection messages
        self.frame_height = 480  # Updated from detection messages
        self._identity_load_time = 0  # Timestamp of last identity load
        self.tracked_vehicles: dict[str, TrackedVehicle] = {}  # vehicle_id → TrackedVehicle
        # Ghost vehicles — recently-lost vehicles kept alive for re-association
        # so a single car driving through a dead-zone doesn't fire detected →
        # left → detected (three events for one car). Keyed by vehicle_id,
        # value is (TrackedVehicle, timestamp_when_ghosted). Expired ghosts
        # emit vehicle_left at expiry time, not the moment they went stale.
        self._ghost_vehicles: dict[str, tuple] = {}
        self._next_vehicle_id = 1  # Simple incrementing ID counter
        self.vehicle_idle_timeout = VEHICLE_IDLE_TIMEOUT  # Hot-reloadable via Redis config
        self.suppress_known = False  # Hot-reloadable: skip alerts for identified people
        # Whether face-recognition is enabled for this camera (read from
        # cameras:registry once at startup). When True, person_appeared
        # is deferred by IDENTITY_GRACE_SECONDS so the face-recognizer
        # has time to identify the person — gives us a single
        # `person_identified` event instead of `appeared (Unknown)` + a
        # follow-up `identified` for known faces. When False we announce
        # immediately because deferring would just add dead time.
        self.face_recognition_enabled = self._read_face_recognition_flag()

    def _read_face_recognition_flag(self) -> bool:
        """One-shot read of `cameras:registry[CAMERA_ID].detect_faces`."""
        try:
            raw = self.r.hget("cameras:registry", CAMERA_ID)
            if not raw:
                return True  # registry missing → default on
            entry = json.loads(raw if isinstance(raw, str) else raw.decode())
            enabled = bool(entry.get("detect_faces", True))
            logger.info(
                f"Face recognition for {CAMERA_ID}: "
                f"{'enabled' if enabled else 'disabled'} "
                f"(grace period for person_appeared "
                f"{'will' if enabled else 'will NOT'} be applied)"
            )
            return enabled
        except Exception as e:
            logger.warning(f"Could not read detect_faces flag: {e} — defaulting to enabled")
            return True

    def _generate_id(self) -> str:
        """Generate a short, readable person ID."""
        pid = f"person_{self.next_id:04d}"
        self.next_id += 1
        return pid

    def _process_vehicle_detections(
        self, detections: list, timestamp: float,
        frame_bytes: bytes = None, hd_frame_bytes: bytes = None,
    ):
        """
        Track vehicles across frames using IoU matching and emit events.

        For each incoming detection:
        1. Match to existing tracked vehicles using IoU
        2. If matched → update state, check for idle timeout
        3. If new → create TrackedVehicle, emit vehicle_detected
        4. Prune stale vehicles not seen for VEHICLE_LOST_TIMEOUT

        Emits:
        - vehicle_detected: when a new vehicle first appears
        - vehicle_idle: when a vehicle stays in roughly the same spot
                        for > VEHICLE_IDLE_TIMEOUT seconds

        `hd_frame_bytes` is the HD-stream frame paired with this batch of
        detections by vehicle-detector at emit time. Cached on the matched
        TrackedVehicle so the next vehicle_sample event can write it to a
        per-sample snapshot key for vehicle-attributes to consume — pairs
        bbox + HD frame from the same moment instead of the attribute
        service doing its own (drift-prone) frame_hd lookup later.
        """
        # --- Step 1: Match incoming detections to tracked vehicles via IoU ---
        for det in detections:
            bbox = det.get("bbox", [0, 0, 0, 0])
            class_name = det.get("class_name", "vehicle")
            confidence = det.get("confidence", 0)

            # Skip vehicles in dead zones
            if self._check_in_dead_zone(bbox):
                continue

            best_match_id = None

            # Pass 1 — IoM rescue for idle/stationary tracks runs BEFORE
            # the primary IoU loop. Idle tracks are "sticky": when a
            # detection near-perfectly aligns with an idle bbox (IoM ≥
            # VEHICLE_IDLE_IOM_THRESHOLD and area ratio ≤ ...IOM_AREA_RATIO_MAX),
            # it absorbs into the idle track. Reordered from "after primary
            # loop" because the post-loop position lost to non-idle tracks
            # that happened to overlap a little (observed live at 16:44:
            # a fresh truck track at IoU 0.40 stole the Honda's
            # bbox-jittered detections, eventually spawning a phantom
            # vehicle_0003 that re-fired vehicle_idle 150s later).
            best_match_id = self._try_idle_iom_match(bbox, class_name)

            # Pass 1b — IoM rescue for idle GHOST tracks. Runs before the
            # primary IoU loop so a non-idle live track can't claim a
            # detection that belongs to a recently-ghosted idle track.
            # Observed live: when a passing pickup spawned vehicle_0003
            # and the real Honda track (vehicle_0001) went to ghost from
            # the 10 s lost-timeout during the pickup's transit, vehicle_0003's
            # bbox shifted onto the Honda's position and absorbed all subsequent
            # Honda-bbox detections via the primary IoU loop. vehicle_0001 stayed
            # in the ghost buffer for the full idle-ghost TTL (600 s) while
            # vehicle_0003 became "the new idle Honda" 150 s later and fired a
            # duplicate vehicle_idle.
            if not best_match_id:
                ghost_id = self._try_idle_iom_ghost_match(bbox, class_name)
                if ghost_id:
                    veh, _ = self._ghost_vehicles.pop(ghost_id)
                    veh.update(bbox, class_name, confidence, timestamp)
                    self.tracked_vehicles[ghost_id] = veh
                    logger.info(
                        f"vehicle {ghost_id} re-associated via idle IoM "
                        f"ghost rescue (no new vehicle_detected emitted)"
                    )
                    continue  # skip rest of loop for this detection

            # Pass 2 — regular IoU loop, only if IoM rescue didn't match.
            #
            # Idle-confirmed tracks demand a tighter IoU before accepting
            # a new detection. A parked car's bbox is fixed; a real
            # re-detection of the same car overlaps near-perfectly. A
            # drive-by passing through that bbox region used to merge in
            # at IoU ~0.25–0.45, polluting the parked car's crop buffer
            # with crops of a different physical vehicle.
            #
            # Gate on `is_stationary or idle_alerted`, not idle_alerted
            # alone — is_stationary flips True after ~5 center-history
            # samples (~1 s) once the track stops moving, while
            # idle_alerted only fires after vehicle_idle_timeout (150s).
            if not best_match_id:
                best_iou = VEHICLE_IOU_THRESHOLD
                det_area = max(0, bbox[2] - bbox[0]) * max(0, bbox[3] - bbox[1])
                for vid, veh in self.tracked_vehicles.items():
                    iou = compute_iou(bbox, veh.bbox)
                    tight_iou = veh.idle_alerted or veh.is_stationary
                    threshold = (VEHICLE_IDLE_IOU_THRESHOLD
                                 if tight_iou
                                 else VEHICLE_IOU_THRESHOLD)

                    # Size-ratio sanity gate for STALE tracks. A non-idle
                    # track that's been silent for > VEHICLE_MATCH_STALE_SECS
                    # must not claim a detection whose bbox area is wildly
                    # different from its own — that's almost certainly a
                    # different physical vehicle entering the same screen
                    # region (live regression: small car at 15:09:45 lost
                    # detection for 10 s, then a school bus arrived at IoU
                    # 0.33 with area 6× the car's, got merged in and
                    # polluted the classifier vote).
                    if (timestamp - veh.last_seen) > VEHICLE_MATCH_STALE_SECS:
                        veh_area = max(0, veh.bbox[2] - veh.bbox[0]) * max(0, veh.bbox[3] - veh.bbox[1])
                        if det_area > 0 and veh_area > 0:
                            area_ratio = max(det_area, veh_area) / min(det_area, veh_area)
                            if area_ratio > VEHICLE_MATCH_AREA_RATIO_MAX:
                                continue

                    if iou > best_iou and iou >= threshold:
                        best_iou = iou
                        best_match_id = vid

            # IoU match can fail across consecutive frames when a fast-moving
            # car shifts by more than half its width — IoU drops below
            # VEHICLE_IOU_THRESHOLD even though it's clearly the same car.
            # Mirror _try_ghost_match's center-distance heuristic here for
            # the live-track case. Same-class only; same VEHICLE_GHOST_MAX_DIST_RATIO
            # threshold. Catches the "drive-by car briefly splits into two
            # TrackedVehicles" bug reported on cam1 (bboxes 50px apart on
            # consecutive frames, IoU≈0.14). See test
            # test_drive_by_with_low_iou_consecutive_frames_does_not_double_track.
            if not best_match_id:
                best_match_id = self._try_live_center_match(
                    bbox, class_name, current_ts=timestamp,
                )

            # Try ghost re-association before treating as a brand-new vehicle.
            # A ghost is a recently-lost vehicle (within VEHICLE_GHOST_TTL).
            # If the new detection is close enough in space + same class, we
            # revive it under its original ID and DO NOT emit vehicle_detected
            # again — this is the same car re-emerging from a dead-zone or a
            # brief occlusion.
            if not best_match_id:
                ghost_id = self._try_ghost_match(bbox, class_name, timestamp)
                if ghost_id:
                    veh, _ = self._ghost_vehicles.pop(ghost_id)
                    veh.update(bbox, class_name, confidence, timestamp)
                    self.tracked_vehicles[ghost_id] = veh
                    logger.info(
                        f"vehicle {ghost_id} re-associated from ghost buffer "
                        f"(class={class_name}, no new vehicle_detected emitted)"
                    )
                    continue  # skip new-vehicle branch below

            if best_match_id:
                # --- Existing vehicle: update state ---
                veh = self.tracked_vehicles[best_match_id]
                veh.update(bbox, class_name, confidence, timestamp)
                # Stash the HD frame paired with THIS bbox so the next
                # vehicle_sample emit can write a per-sample HD snapshot
                # key. Pairs bbox+HD-frame from the same moment.
                if hd_frame_bytes:
                    veh.last_hd_frame_bytes = hd_frame_bytes

                # Store/update snapshot if frame bytes provided
                if frame_bytes and not veh.snapshot_key:
                    # Millisecond resolution to keep two cars that arrive in
                    # the same second from overwriting each other's snapshots.
                    # The key shape is stable: dashboards read whatever
                    # snapshot_key the event payload carries, so producer-side
                    # resolution can widen without consumer changes.
                    snap_ts = int(veh.first_seen * 1000)
                    snap_key = stream_key(_VSNAP_TMPL, camera_id=CAMERA_ID, timestamp=snap_ts)
                    bbox_key = stream_key(_VSNAP_BBOX_TMPL, camera_id=CAMERA_ID, timestamp=snap_ts)
                    self.r.setex(snap_key, 86400, frame_bytes)
                    self.r.setex(bbox_key, 86400, json.dumps(bbox))
                    veh.snapshot_key = snap_key
                    veh.snapshot_bbox = bbox  # Store bbox matching the snapshot frame

                # Check for idle timeout — only if vehicle is actually stationary
                if (veh.duration >= self.vehicle_idle_timeout
                        and veh.is_stationary
                        and not veh.idle_alerted):
                    veh.idle_alerted = True
                    self._emit_vehicle_idle_event(veh, timestamp)

                # Emit a sampling event so vehicle-attributes-cam{N} can pull
                # the HD frame for this track. For the first EAGER_SAMPLE_FRAMES
                # matched updates, emit every frame (short drive-bys would
                # otherwise lose 2/3 of their detector hits to the throttle).
                # After that, fall back to the every-Nth throttle to bound
                # Redis HD-sample write cost on long parked-car tracks.
                #
                # Occlusion gate: if another currently-tracked MOVING vehicle's
                # bbox overlaps this track's bbox, skip the sample. Otherwise
                # the parked car's classifier buffer gets crops with two
                # vehicles visible (parked one + foreground intruder).
                if (
                    EMIT_VEHICLE_SAMPLES
                    and (veh.frame_count <= EAGER_SAMPLE_FRAMES
                         or veh.frame_count % SAMPLE_INTERVAL_FRAMES == 0)
                    and self._bbox_area(veh.bbox) >= MIN_SAMPLE_BBOX_AREA_SUB_PX
                    and not self._sample_occluded_by_moving_vehicle(veh)
                ):
                    self._emit_vehicle_sample_event(veh, timestamp)

            else:
                # --- New vehicle: create tracker and emit detection event ---
                vid = f"vehicle_{self._next_vehicle_id:04d}"
                self._next_vehicle_id += 1

                veh = TrackedVehicle(vid, bbox, class_name, confidence, timestamp)
                if hd_frame_bytes:
                    veh.last_hd_frame_bytes = hd_frame_bytes

                # Store snapshot in Redis with 24h TTL
                if frame_bytes:
                    # Millisecond resolution — see note in the existing-vehicle
                    # branch above. Prevents collisions for two cars arriving
                    # in the same second on the same camera.
                    snap_ts = int(timestamp * 1000)
                    snap_key = stream_key(_VSNAP_TMPL, camera_id=CAMERA_ID, timestamp=snap_ts)
                    bbox_key = stream_key(_VSNAP_BBOX_TMPL, camera_id=CAMERA_ID, timestamp=snap_ts)
                    self.r.setex(snap_key, 86400, frame_bytes)
                    self.r.setex(bbox_key, 86400, json.dumps(bbox))
                    veh.snapshot_key = snap_key
                    veh.snapshot_bbox = bbox

                self.tracked_vehicles[vid] = veh

                # Emit on first sighting. Removed the old global rate-limit
                # — IoU matching already prevents same-vehicle duplicates,
                # and the global timer was dropping legitimate events when
                # two vehicles arrived within 3 seconds of each other.
                self._emit_vehicle_detected_event(veh, timestamp)

                # Also emit a sample for the spawn frame so vehicle-attributes
                # captures the very first crop. Without this, a 1-detection
                # track produces 0 buffer entries (empty buffer skips flush
                # in storage.py) and a 2-detection track produces 1. The
                # spawn sample is independent of EAGER_SAMPLE_FRAMES — that
                # constant only governs the existing-vehicle branch throttle.
                # Occlusion gate applies here too — if a fast-moving vehicle
                # spawns inside another moving vehicle's bbox (e.g., two
                # cars overlapping at frame edge), skip the spawn sample.
                if (
                    EMIT_VEHICLE_SAMPLES
                    and self._bbox_area(veh.bbox) >= MIN_SAMPLE_BBOX_AREA_SUB_PX
                    and not self._sample_occluded_by_moving_vehicle(veh)
                ):
                    self._emit_vehicle_sample_event(veh, timestamp)

        # --- Step 2: Move stale vehicles to ghost buffer (deferred vehicle_left) ---
        # Instead of firing vehicle_left immediately when a vehicle goes stale,
        # we move it to _ghost_vehicles. If the same vehicle re-appears within
        # VEHICLE_GHOST_TTL seconds, we re-associate (no leave event ever fires).
        # If it doesn't, we emit vehicle_left at ghost expiry.
        #
        # Per-track timeout: idle/stationary tracks use VEHICLE_LOST_TIMEOUT
        # (10 s — handles detector stutter on parked cars). Non-idle moving
        # tracks use VEHICLE_LOST_TIMEOUT_DRIVING (3 s) so a brief detection
        # of one vehicle can't sit in tracked_vehicles long enough to be
        # grabbed by a totally different vehicle entering the same screen
        # region later.
        def _track_lost_timeout(veh) -> float:
            return (VEHICLE_LOST_TIMEOUT
                    if (veh.idle_alerted or veh.is_stationary)
                    else VEHICLE_LOST_TIMEOUT_DRIVING)
        stale_ids = [
            vid for vid, veh in self.tracked_vehicles.items()
            if timestamp - veh.last_seen > _track_lost_timeout(veh)
        ]
        for vid in stale_ids:
            veh = self.tracked_vehicles.pop(vid)
            self._ghost_vehicles[vid] = (veh, timestamp)

        # --- Step 2b: Expire ghosts past TTL and emit the track-end events ---
        # `vehicle_gone` always fires (internal — used by vehicle-attributes
        # as the buffer-flush trigger for both drive-bys and idle-leaves).
        # `vehicle_left` fires ONLY when the vehicle had previously gone idle —
        # drive-by cars never set idle_alerted, so they no longer spam the
        # events panel + Telegram with exit events. See contracts/streams.py
        # comment on VEHICLE_GONE_EVENT.
        # Idle-confirmed tracks get a much longer ghost window because the
        # detector intermittently misses parked cars (RTSP/frame_hd hiccups,
        # brief obstruction). Without this, the same parked car spawns a new
        # track every gap > 40 s — observed live on cam1: identical bbox
        # producing vehicle_0011 → 0022 → 0029 → 0037 over 17 min.
        expired_ghost_ids = []
        for vid, (veh, ghost_ts) in self._ghost_vehicles.items():
            ttl = VEHICLE_IDLE_GHOST_TTL if veh.idle_alerted else VEHICLE_GHOST_TTL
            if timestamp - ghost_ts > ttl:
                expired_ghost_ids.append(vid)
        for vid in expired_ghost_ids:
            veh, _ = self._ghost_vehicles.pop(vid)
            self._emit_vehicle_gone_event(veh, timestamp)
            if veh.idle_alerted:
                self._emit_vehicle_left_event(veh, timestamp)

    def _update_state(self):
        """
        Update the Redis state key with the current scene snapshot.

        This is a single key (not a stream) that the dashboard reads to show
        who is currently in the frame RIGHT NOW. Overwritten on every update.

        Filters out people whose bbox sits entirely inside a dead zone —
        the dashboard's overlay also skips drawing them, so counting them
        in `num_people` produced a "ghost count" mismatch (UI shows
        "1 person" with no bbox visible).
        """
        visible = [
            p for p in self.tracked.values()
            if not self._check_in_dead_zone(p.bbox)
        ]
        state = {
            "camera_id": CAMERA_ID,
            "timestamp": str(time.time()),
            "num_people": str(len(visible)),
            "people": json.dumps([p.to_dict() for p in visible]),
        }
        self.r.hset(STATE_KEY, mapping=state)

    def update(self, detections: list[dict], timestamp: float, frame_bytes: bytes | None = None):
        """
        Process a new set of detections and update tracked people.

        `frame_bytes` is the JPEG-encoded frame the detector ran on, shipped
        on the detection-stream message (mirror of the vehicle path). It's
        buffered onto every TrackedPerson that gets matched or created in
        this update so the person_appeared snapshot can use the exact frame
        the bbox came from — preventing the "bbox on empty floor where the
        person walked away from" symptom.

        Algorithm:
        1. For each detection, find the best IoU match among tracked people
        2. If match > threshold → update that tracked person's state
        3. If no match → create a new tracked person
        4. Check for lost people (not seen for LOST_TIMEOUT seconds)
        """
        current_time = timestamp if timestamp > 0 else time.time()

        # --- Step 1: Match detections to existing tracks ---
        matched_track_ids = set()
        unmatched_detections = []

        for det in detections:
            bbox = det["bbox"]

            # Skip tiny detections (distant people, YOLO artifacts)
            bbox_area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
            if bbox_area < MIN_BBOX_AREA:
                continue

            # Skip detections in dead zones
            if self._check_in_dead_zone(bbox):
                continue

            best_iou = 0.0
            best_track_id = None

            for track_id, person in self.tracked.items():
                iou = compute_iou(bbox, person.bbox)
                if iou > best_iou:
                    best_iou = iou
                    best_track_id = track_id

            # Per-track IoU threshold: identified tracks use the looser
            # IDENTITY_TRACK_IOU_THRESHOLD (default 0.10) so an
            # identified person walking far away — bbox shrinks and
            # shifts rapidly — keeps their track ID + identity instead
            # of getting destroyed and re-spawned as Unknown. Plain
            # tracks keep the stricter self.iou_threshold so noise blobs
            # don't latch onto unrelated detections.
            match_threshold = self.iou_threshold
            if (best_track_id is not None
                    and self.tracked[best_track_id].identity_name):
                match_threshold = min(match_threshold, IDENTITY_TRACK_IOU_THRESHOLD)

            if best_iou >= match_threshold and best_track_id not in matched_track_ids:
                # Identity-expiry guard. If this track was silent for
                # > IDENTITY_PERSIST_GAP_SECS and the gap had no face-
                # recognizer confirmation, demote identity_name before
                # the update — face-recognizer's next cycle will
                # restore it (correct name) or leave it blank (if a
                # stranger took the spot). Prevents the bug where a
                # stranger inheriting a 20 s-stale bbox keeps the
                # original person's name.
                person_for_check = self.tracked[best_track_id]
                gap = current_time - person_for_check.last_seen
                if (gap > IDENTITY_PERSIST_GAP_SECS
                        and person_for_check.identity_name
                        and person_for_check.last_identity_confirmation_ts
                        <= person_for_check.last_seen):
                    logger.info(
                        f"identity '{person_for_check.identity_name}' "
                        f"demoted on re-match (gap={gap:.1f}s > "
                        f"{IDENTITY_PERSIST_GAP_SECS}s; "
                        f"awaiting face-recognizer re-confirmation)"
                    )
                    person_for_check.identity_name = ""
                    person_for_check._pending_identity = ""
                    person_for_check._pending_identity_count = 0

                # Match found — update existing track (pass keypoints for action detection)
                prev_action = self.tracked[best_track_id].update(
                    bbox, current_time, keypoints=det.get("keypoints")
                )
                matched_track_ids.add(best_track_id)
                # Pair the bbox with the frame it was computed from — used at
                # event-emit time so the snapshot shows the person where the
                # bbox says they are. Without this, _save_person_snapshot
                # grabs the LATEST frame and the bbox is from N frames ago.
                if frame_bytes:
                    self.tracked[best_track_id].last_frame_bytes = frame_bytes

                # Emit "person_appeared" on first stable detection.
                #
                # When face-recognition is enabled for this camera, ALWAYS
                # defer by IDENTITY_GRACE_SECONDS so the face-recognizer
                # has time to identify the person first. If identification
                # lands inside the window, `_update_identities` fires a
                # single `person_identified` event and the grace block at
                # the bottom of update() skips the appeared event — this
                # eliminates the old "Unknown appeared then Alice
                # identified" dual-alert flow.
                #
                # When face-recognition is OFF (registry has
                # detect_faces=false), deferring would just be dead time,
                # so we announce immediately.
                person = self.tracked[best_track_id]
                if (not person.announced
                        and person.announce_after is None
                        and person.frame_count >= 15):
                    if self.face_recognition_enabled:
                        person.announce_after = current_time + IDENTITY_GRACE_SECONDS
                    else:
                        self._emit_event("person_appeared", person, current_time)
                        person.announced = True
                elif (person.announced
                      and prev_action != person.action
                      and prev_action not in ("unknown", "")
                      and person.action not in ("unknown", "")
                      and current_time - person._last_action_event_ts
                          >= TrackedPerson._ACTION_EVENT_COOLDOWN_SEC):
                    # Action changed — emit transition event (with
                    # per-person cooldown so a borderline pose doesn't
                    # spam the feed when it oscillates).
                    self._emit_event("action_changed", person, current_time,
                                     extra={"prev_action": prev_action})
                    person._last_action_event_ts = current_time
            else:
                # No match — save for new track creation
                unmatched_detections.append(det)

        # --- Step 2: Create new tracks for unmatched detections ---
        for det in unmatched_detections:
            person_id = self._generate_id()
            person = TrackedPerson(person_id, det["bbox"], current_time)
            if frame_bytes:
                person.last_frame_bytes = frame_bytes
            self.tracked[person_id] = person

        # --- Step 3: Check for lost people ---
        # Per-track lost timeout: identified tracks survive longer
        # silent gaps (IDENTITY_LOST_TIMEOUT, default 30 s) so a
        # known person walking off + back within ~30 s keeps their
        # track ID + identity. Plain tracks keep the stricter
        # self.lost_timeout (8 s default) so noise tracks expire fast.
        lost_ids = []
        for track_id, person in self.tracked.items():
            time_since_seen = current_time - person.last_seen
            lost_threshold = (IDENTITY_LOST_TIMEOUT
                              if person.identity_name
                              else self.lost_timeout)
            if time_since_seen > lost_threshold:
                # Person has left the frame
                if person.announced:
                    self._emit_event("person_left", person, current_time)
                lost_ids.append(track_id)

        for track_id in lost_ids:
            del self.tracked[track_id]

        # --- Step 4: Update identities from face recognizer ---
        self._update_identities()

        # --- Step 5: Check deferred announcements (identity grace period) ---
        for person in self.tracked.values():
            if person.announce_after is not None and not person.announced:
                if person.identity_name:
                    # Known person identified during grace period — skip announce
                    person.announced = True
                    person.announce_after = None
                    logger.info(
                        f"Grace period: suppressed person_appeared for known "
                        f"'{person.identity_name}' ({person.person_id})"
                    )
                elif current_time >= person.announce_after:
                    # Grace period expired, still unknown — announce now
                    self._emit_event("person_appeared", person, current_time)
                    person.announced = True
                    person.announce_after = None

        # --- Step 6: Update scene state in Redis ---
        self._update_state()


# Alias used by tests and the vehicle-attributes service that want to
# instantiate the tracker without hard-coding the class name. The canonical
# name stays PersonTracker for backward compat with direct imports.
Manager = PersonTracker
