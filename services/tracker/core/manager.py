"""tracker/core/manager.py — PersonTracker orchestrator class.

Owns the dictionaries of currently-tracked people + vehicles, runs the
IoU matching, emits events to Redis, and keeps the camera state hash
in sync. The big one — ~700 lines of pipeline logic.
"""

import json
import os
import time

import redis

from .config import (
    logger,
    CAMERA_ID,
    EVENT_STREAM,
    STATE_KEY,
    ZONE_KEY,
    IDENTITY_KEY,
    FRAME_STREAM,
    VEHICLE_SNAPSHOT_KEY_TMPL as _VSNAP_TMPL,
    VEHICLE_SNAPSHOT_BBOX_KEY_TMPL as _VSNAP_BBOX_TMPL,
    PERSON_SNAPSHOT_KEY_TMPL as _PSNAP_TMPL,
    MAX_EVENT_STREAM_LEN,
    VEHICLE_IDLE_TIMEOUT,
    VEHICLE_LOST_TIMEOUT,
    VEHICLE_IOU_THRESHOLD,
    VEHICLE_IDLE_IOU_THRESHOLD,
    VEHICLE_IDLE_IOM_THRESHOLD,
    VEHICLE_IDLE_IOM_AREA_RATIO_MAX,
    VEHICLE_GHOST_TTL,
    VEHICLE_IDLE_GHOST_TTL,
    VEHICLE_GHOST_MAX_DIST_RATIO,
    MIN_BBOX_AREA,
    IDENTITY_GRACE_SECONDS,
    VEHICLE_SAMPLE_EVENT,
    VEHICLE_GONE_EVENT,
    stream_key,
    point_in_polygon,
    should_alert,
    get_time_period,
)
from .iou import compute_iou
from .state import TrackedPerson, TrackedVehicle

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

# YOLOv8 frequently flips a single vehicle between "car", "truck", and
# "bus" across consecutive frames (especially trucks/SUVs at partial
# occlusion, vans seen end-on, large sedans at angle). The fallback
# match paths used to require strict class equality, which split one
# physical drive-by into two tracks (observed live: a red pickup at
# 11:35 became vehicle_0009 "car" + vehicle_0010 "truck" 2 s later,
# neither got full per-track samples).
#
# 4-wheel motor vehicles (car, truck, bus) are treated as
# interchangeable in the fallback paths. Bicycle + motorcycle stay
# strict — they're visually distinct enough that YOLO almost never
# confuses them with 4-wheel vehicles, and false-merging a passing
# motorcycle into a car track would be a real data error.
_VEHICLE_CLASS_EQUIV = {"car", "truck", "bus"}


def _class_compatible(a: str, b: str) -> bool:
    """True if two YOLO class labels can plausibly be the same physical
    vehicle observed across frames (YOLO class flicker tolerance)."""
    if a == b:
        return True
    return a in _VEHICLE_CLASS_EQUIV and b in _VEHICLE_CLASS_EQUIV


class PersonTracker:
    """
    Tracks people across frames using IoU matching.

    Maintains a dictionary of currently tracked people. On each new set of
    detections, matches them to existing tracks or creates new ones.
    Emits events when people appear or leave.
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

    def _emit_event(self, event_type: str, person: TrackedPerson, timestamp: float, extra: dict = None):
        """Publish an event to the events stream."""
        # Determine which zone the person is in
        zone_name, alert_level = self._find_zone(person.bbox)

        # Evaluate zone + time-of-day rules to decide if this should trigger an alert
        alert_triggered = should_alert(alert_level) if alert_level else False

        event = {
            "camera_id": CAMERA_ID,
            "event_type": event_type,
            "timestamp": str(timestamp),
            "person_id": person.person_id,
            "identity_name": person.identity_name,
            "duration": str(round(person.duration, 1)),
            "direction": person.direction,
            "action": person.action,
            "bbox": json.dumps(person.bbox),
            "frame_count": str(person.frame_count),
            "zone": zone_name,
            "alert_level": alert_level,
            "alert_triggered": str(alert_triggered),
            "time_period": get_time_period(),
        }

        # Save a snapshot at event emission time for person events
        # so the dashboard uses the correct frame, not the (stale) live frame.
        # Also save the bbox that matches the snapshot frame so the dashboard
        # draws the box in the right place (not a stale detection bbox).
        if event_type in ("person_appeared", "person_identified"):
            # Pass the buffered frame_bytes (paired with `person.bbox` at
            # detection time) so the snapshot frame matches the bbox. Falls
            # back to grabbing the latest frame if we don't have buffered
            # bytes (e.g. pose-detector hasn't been upgraded yet).
            snap_key = self._save_person_snapshot(
                timestamp, person.bbox, frame_bytes=person.last_frame_bytes,
            )
            if snap_key:
                event["snapshot_key"] = snap_key
                event["snapshot_bbox"] = json.dumps(person.bbox)

        if extra:
            event.update(extra)

        self.r.xadd(EVENT_STREAM, event, maxlen=MAX_EVENT_STREAM_LEN)
        self.total_events += 1

        zone_str = f" | zone={zone_name}" if zone_name else ""
        name_str = f" ({person.identity_name})" if person.identity_name else ""
        logger.info(
            f"EVENT: {event_type} | {person.person_id}{name_str} | "
            f"action={person.action} | "
            f"duration={person.duration:.1f}s | direction={person.direction}"
            f"{zone_str}"
        )

    def _save_person_snapshot(self, timestamp: float, bbox: list = None,
                                frame_bytes: bytes | None = None) -> str | None:
        """
        Save the JPEG frame that the bbox was computed from, plus the bbox
        itself, to Redis so the dashboard can render the notification with
        the box drawn over the right pixels.

        `frame_bytes` is the preferred input — passed in from
        TrackedPerson.last_frame_bytes, which is the exact frame the
        pose-detector ran on when it produced `bbox`. With the 4-second
        announce grace period there's a wide gap between detection time
        and emit time, so falling back to "latest frame in the stream"
        (the old behavior, kept as a defensive fallback) draws the bbox
        on a frame from several seconds AFTER the detection. For a moving
        person that gap is the "bbox on empty floor" symptom.

        Returns the Redis key or None if no frame available.
        Uses 2h TTL (matches dashboard snapshot cleanup).
        """
        try:
            # Prefer the frame bytes paired with this detection. Fall back
            # to xrevrange only if the caller didn't pass any (older
            # detector versions, or pre-detection event types).
            if not frame_bytes:
                entries = self.r.xrevrange(FRAME_STREAM.encode(), count=1)
                if entries:
                    frame_bytes = entries[0][1].get(b"frame") or entries[0][1].get(b"frame_bytes")
            if not frame_bytes:
                return None

            snap_key = stream_key(_PSNAP_TMPL, camera_id=CAMERA_ID, timestamp=int(timestamp))
            self.r.setex(snap_key, 7200, frame_bytes)  # 2h TTL

            # Save companion bbox key so dashboard draws box in the right place
            if bbox:
                bbox_key = f"{snap_key}:bbox"
                self.r.setex(bbox_key, 7200, json.dumps(bbox))

            return snap_key
        except Exception as e:
            logger.debug(f"Person snapshot save failed: {e}")
            return None

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

            # Try to match to existing tracked vehicle
            best_match_id = None
            best_iou = VEHICLE_IOU_THRESHOLD

            for vid, veh in self.tracked_vehicles.items():
                iou = compute_iou(bbox, veh.bbox)
                # Idle-confirmed tracks demand a tighter IoU before accepting
                # a new detection. A parked car's bbox is fixed; a real
                # re-detection of the same car overlaps near-perfectly. A
                # drive-by passing through that bbox region used to merge in
                # at IoU ~0.25–0.45, polluting the parked car's crop buffer
                # with crops of a different physical vehicle. Observed live:
                # vehicle_0001 captured 7 crops of a parked car + 1 crop of
                # an SUV that drove through that spot, then the classifier
                # vote split between them and body_type came out at conf
                # 0.54 (just under the 0.55 threshold).
                #
                # Gate on `is_stationary or idle_alerted`, not idle_alerted
                # alone — is_stationary flips True after ~5 center-history
                # samples (~1 s) once the track stops moving, while
                # idle_alerted only fires after vehicle_idle_timeout (150s).
                # Without is_stationary in the gate, a freshly-parked car
                # is protected only after 150 s of accumulating pollution:
                # vehicle_0011 had two passing-car bboxes merged into its
                # crops at +1 s and +40 s, both well before idle_alerted.
                tight_iou = veh.idle_alerted or veh.is_stationary
                threshold = (VEHICLE_IDLE_IOU_THRESHOLD
                             if tight_iou
                             else VEHICLE_IOU_THRESHOLD)
                if iou > best_iou and iou >= threshold:
                    best_iou = iou
                    best_match_id = vid

            # IoM (intersection-over-min) escape hatch for idle tracks
            # ONLY. The tight idle IoU gate (0.65) rejects detections whose
            # bbox is the same parked car with detector jitter widening it
            # by ~30 px (curb shadow, adjacent vehicle clipping). Without
            # the escape hatch, a phantom track spawns directly on top of
            # the idle car and fires a duplicate vehicle_idle 150s later.
            # IoM threshold is high (0.9) and we require the area ratio
            # to be ≤2× so a person standing inside a parked-truck bbox
            # can't false-merge (person area is ~1% of truck area).
            if not best_match_id:
                best_match_id = self._try_idle_iom_match(bbox, class_name)

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
                if EMIT_VEHICLE_SAMPLES and (
                    veh.frame_count <= EAGER_SAMPLE_FRAMES
                    or veh.frame_count % SAMPLE_INTERVAL_FRAMES == 0
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
                if EMIT_VEHICLE_SAMPLES:
                    self._emit_vehicle_sample_event(veh, timestamp)

        # --- Step 2: Move stale vehicles to ghost buffer (deferred vehicle_left) ---
        # Instead of firing vehicle_left immediately when a vehicle goes stale,
        # we move it to _ghost_vehicles. If the same vehicle re-appears within
        # VEHICLE_GHOST_TTL seconds, we re-associate (no leave event ever fires).
        # If it doesn't, we emit vehicle_left at ghost expiry.
        stale_ids = [
            vid for vid, veh in self.tracked_vehicles.items()
            if timestamp - veh.last_seen > VEHICLE_LOST_TIMEOUT
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

    def _try_idle_iom_match(self, bbox: list, class_name: str) -> str | None:
        """Borderline-IoU rescue for idle/stationary tracks.

        The tight idle IoU gate (VEHICLE_IDLE_IOU_THRESHOLD=0.65) rejects
        detections whose bbox is the same parked car but YOLO-jittered
        slightly wider/taller. Without this, a jittered detection spawns
        a phantom track on top of the idle car and fires a duplicate
        vehicle_idle 150s later (observed live at 12:25 on cam1).

        Returns the matched vehicle_id when:
          1. an existing track is idle or stationary,
          2. the detection's class is compatible (car↔truck↔bus equivalence
             from #41 applies, so bbox-jitter on the same vehicle won't
             miss when YOLO also flips class), and
          3. intersection-over-min ≥ VEHICLE_IDLE_IOM_THRESHOLD AND the
             two bboxes are within VEHICLE_IDLE_IOM_AREA_RATIO_MAX in area
             (rules out person/cyclist inside parked-truck bbox).

        Returns None otherwise. Does not modify state.
        """
        a_x1, a_y1, a_x2, a_y2 = bbox
        a_area = (a_x2 - a_x1) * (a_y2 - a_y1)
        if a_area <= 0:
            return None

        best_vid = None
        best_iom = VEHICLE_IDLE_IOM_THRESHOLD  # only beat the threshold

        for vid, veh in self.tracked_vehicles.items():
            if not (veh.idle_alerted or veh.is_stationary):
                continue
            if not _class_compatible(veh.class_name, class_name):
                continue
            b_x1, b_y1, b_x2, b_y2 = veh.bbox
            b_area = (b_x2 - b_x1) * (b_y2 - b_y1)
            if b_area <= 0:
                continue

            # Area-ratio gate first — cheap rejection of person-in-truck shapes.
            area_ratio = max(a_area, b_area) / min(a_area, b_area)
            if area_ratio > VEHICLE_IDLE_IOM_AREA_RATIO_MAX:
                continue

            ix1 = max(a_x1, b_x1)
            iy1 = max(a_y1, b_y1)
            ix2 = min(a_x2, b_x2)
            iy2 = min(a_y2, b_y2)
            if ix2 <= ix1 or iy2 <= iy1:
                continue
            i_area = (ix2 - ix1) * (iy2 - iy1)
            iom = i_area / min(a_area, b_area)

            if iom >= best_iom:
                best_iom = iom
                best_vid = vid

        return best_vid

    def _try_live_center_match(self, bbox: list, class_name: str,
                                current_ts: float = 0.0) -> str | None:
        """Fallback live-track match by center distance when IoU failed.

        When a vehicle drifts fast enough that consecutive-frame bboxes have
        IoU below VEHICLE_IOU_THRESHOLD, the standard match step misses it
        and the tracker spawns a new TrackedVehicle for the same physical
        car. This helper checks whether any currently-tracked vehicle of the
        SAME class is within `bbox_w * VEHICLE_GHOST_MAX_DIST_RATIO` of the
        new bbox's center; if so, return its id so the match step reuses it.

        Same-class only — mirrors the ghost-match's safety rule. Cars
        don't morph into trucks mid-track. Note: the standard IoU step
        deliberately doesn't check class (handles YOLO class flicker on
        the same vehicle); we restrict the looser center-distance path
        only.
        """
        if not self.tracked_vehicles:
            return None
        cx = (bbox[0] + bbox[2]) / 2
        cy = (bbox[1] + bbox[3]) / 2
        bbox_w = max(1.0, bbox[2] - bbox[0])
        max_dist = bbox_w * VEHICLE_GHOST_MAX_DIST_RATIO
        best_id = None
        best_dist = max_dist
        for vid, veh in self.tracked_vehicles.items():
            # A track already updated in THIS frame can't be the same
            # physical vehicle as a different detection in the same frame.
            # Two cars side-by-side at different positions both belong to
            # the same `_process_vehicle_detections` call — each must get
            # its own track id. Without this gate the relaxed class-
            # compatibility check below would let the second detection's
            # center-distance fallback merge into the first.
            if current_ts and veh.last_seen >= current_ts:
                continue
            if not _class_compatible(veh.class_name, class_name):
                continue  # bus vs car etc; car↔truck flicker is allowed
            # Idle-confirmed tracks must not be matched via the loose
            # center-distance fallback. This fallback exists for FAST cars
            # whose IoU drops between consecutive frames — parked cars
            # don't move. Letting a parked track match this way would let
            # a drive-by car that already failed the tight idle-IoU check
            # sneak back in via the looser center-distance path. Skipping
            # them here keeps the idle-IoU tightening effective. Use
            # is_stationary (not just idle_alerted) so freshly-parked
            # cars are protected from the moment they stop moving, not
            # 150 s later when idle_alerted finally fires.
            if veh.idle_alerted or veh.is_stationary:
                continue
            vx = (veh.bbox[0] + veh.bbox[2]) / 2
            vy = (veh.bbox[1] + veh.bbox[3]) / 2
            dist = ((cx - vx) ** 2 + (cy - vy) ** 2) ** 0.5
            if dist < best_dist:
                best_dist = dist
                best_id = vid
        return best_id

    def _try_ghost_match(self, bbox: list, class_name: str, timestamp: float) -> str | None:
        """If a recently-departed vehicle is near this bbox, return its id.
        Otherwise None. Class compatibility uses _class_compatible — strict
        equality is too tight because YOLOv8 frequently flickers a single
        drive-by between 'car' and 'truck'."""
        if not self._ghost_vehicles:
            return None
        cx = (bbox[0] + bbox[2]) / 2
        cy = (bbox[1] + bbox[3]) / 2
        bbox_w = max(1.0, bbox[2] - bbox[0])
        max_dist = bbox_w * VEHICLE_GHOST_MAX_DIST_RATIO
        best_id = None
        best_dist = max_dist
        for vid, (veh, _ts) in self._ghost_vehicles.items():
            if not _class_compatible(veh.class_name, class_name):
                continue  # bus/motorcycle/bicycle stay strict
            gx = (veh.bbox[0] + veh.bbox[2]) / 2
            gy = (veh.bbox[1] + veh.bbox[3]) / 2
            dist = ((cx - gx) ** 2 + (cy - gy) ** 2) ** 0.5
            # Idle ghosts: stricter center-distance bound. A parked car
            # ghosted by a brief detector miss should re-attach only if
            # the new detection is essentially at the same spot
            # (≤ 30 % of bbox width). The loose 3.5× threshold is meant
            # for fast-moving drive-by cars; applied to a stationary
            # ghost it would let an unrelated nearby vehicle inherit
            # the parked car's track id.
            idle_max = bbox_w * 0.3
            # Use is_stationary (not just idle_alerted) so a freshly-
            # parked car ghosted by a brief detector miss gets the
            # stricter re-association bound from the moment it stops
            # moving — same rationale as the IoU gate above.
            parked = veh.idle_alerted or veh.is_stationary
            effective_max = idle_max if parked else max_dist
            if dist < effective_max and dist < best_dist:
                best_dist = dist
                best_id = vid
        return best_id

    def _emit_vehicle_detected_event(self, veh: 'TrackedVehicle', timestamp: float):
        """Emit a vehicle_detected event to the events stream."""
        zone_name, alert_level = self._find_zone(veh.bbox)
        alert_triggered = should_alert(alert_level) if alert_level else False

        event = {
            "camera_id": CAMERA_ID,
            "event_type": "vehicle_detected",
            "timestamp": str(timestamp),
            "person_id": "",
            "identity_name": "",
            "duration": "0",
            "direction": "",
            "action": "",
            "bbox": json.dumps(veh.bbox),
            "frame_count": str(veh.frame_count),
            "zone": zone_name,
            "alert_level": alert_level,
            "alert_triggered": str(alert_triggered),
            "vehicle_class": veh.class_name,
            "vehicle_confidence": str(round(veh.confidence, 3)),
            "vehicle_id": veh.vehicle_id,
            "vehicle_first_seen": str(int(veh.first_seen)),
            "snapshot_key": veh.snapshot_key,
            "snapshot_bbox": json.dumps(veh.snapshot_bbox),
            "time_period": get_time_period(),
        }

        self.r.xadd(EVENT_STREAM, event, maxlen=MAX_EVENT_STREAM_LEN)
        self.total_events += 1

        logger.info(
            f"EVENT: vehicle_detected | {veh.class_name} ({veh.confidence:.2f})"
            f"{f' | zone={zone_name}' if zone_name else ''}"
        )

    def _emit_vehicle_sample_event(self, veh: 'TrackedVehicle', timestamp: float):
        """Emit a low-weight sampling event the attribute service uses to
        decide when to crop the current HD frame for this track.

        Mirrors vehicle_detected payload so a consumer can treat both as
        `(track_id, bbox, timestamp)` carriers without branching on event_type.
        """
        # Write the per-sample HD snapshot to Redis with a short TTL so
        # vehicle-attributes-cam{N} crops a frame that's temporally paired
        # with the bbox (avoids the drift bug where `frame_hd:{cam}` may
        # carry a frame from a different moment than when the bbox was
        # computed). Mirror of the v0.2.0 person_appeared snapshot fix.
        hd_snapshot_key = ""
        if veh.last_hd_frame_bytes:
            ts_ms = int(timestamp * 1000)
            hd_snapshot_key = f"vehicle_hd_sample:{CAMERA_ID}:{veh.vehicle_id}:{ts_ms}"
            try:
                self.r.setex(hd_snapshot_key, 60, veh.last_hd_frame_bytes)
            except Exception:
                hd_snapshot_key = ""  # SETEX failed; consumer falls back

        event = {
            "camera_id": CAMERA_ID,
            "event_type": VEHICLE_SAMPLE_EVENT,
            "timestamp": str(timestamp),
            "bbox": json.dumps(veh.bbox),
            "vehicle_class": veh.class_name,
            "vehicle_confidence": str(round(veh.confidence, 3)),
            "vehicle_id": veh.vehicle_id,
            "vehicle_first_seen": str(int(veh.first_seen)),
            "frame_count": str(veh.frame_count),
            "hd_snapshot_key": hd_snapshot_key,
        }
        self.r.xadd(EVENT_STREAM, event, maxlen=MAX_EVENT_STREAM_LEN)

    def _emit_vehicle_idle_event(self, veh: 'TrackedVehicle', timestamp: float):
        """
        Emit a vehicle_idle event when a vehicle has been stationary
        for longer than VEHICLE_IDLE_TIMEOUT.
        """
        zone_name, alert_level = self._find_zone(veh.bbox)
        alert_triggered = should_alert(alert_level) if alert_level else False

        event = {
            "camera_id": CAMERA_ID,
            "event_type": "vehicle_idle",
            "timestamp": str(timestamp),
            "person_id": "",
            "identity_name": "",
            "duration": str(round(veh.duration, 1)),
            "direction": "",
            "action": "",
            "bbox": json.dumps(veh.bbox),
            "frame_count": str(veh.frame_count),
            "zone": zone_name,
            "alert_level": alert_level,
            "alert_triggered": str(alert_triggered),
            "vehicle_class": veh.class_name,
            "vehicle_confidence": str(round(veh.confidence, 3)),
            "vehicle_id": veh.vehicle_id,
            "vehicle_first_seen": str(int(veh.first_seen)),
            "snapshot_key": veh.snapshot_key,
            "snapshot_bbox": json.dumps(veh.snapshot_bbox),
            "time_period": get_time_period(),
        }

        self.r.xadd(EVENT_STREAM, event, maxlen=MAX_EVENT_STREAM_LEN)
        self.total_events += 1

        logger.info(
            f"EVENT: vehicle_idle | {veh.class_name} idling {veh.duration:.1f}s"
            f"{f' | zone={zone_name}' if zone_name else ''}"
        )

    def _emit_vehicle_left_event(self, veh: 'TrackedVehicle', timestamp: float):
        """Emit a vehicle_left event when a tracked vehicle disappears.

        Fires when the vehicle hasn't been detected for VEHICLE_LOST_TIMEOUT
        seconds. `duration` is the full visit length (first_seen → last_seen),
        which is what the dashboard / Telegram feed will display for the
        "car parked here for 2h" use case.
        """
        zone_name, alert_level = self._find_zone(veh.bbox)
        alert_triggered = should_alert(alert_level) if alert_level else False
        visit_duration = veh.last_seen - veh.first_seen

        event = {
            "camera_id": CAMERA_ID,
            "event_type": "vehicle_left",
            "timestamp": str(timestamp),
            "person_id": "",
            "identity_name": "",
            "duration": str(round(visit_duration, 1)),
            "direction": "",
            "action": "",
            "bbox": json.dumps(veh.bbox),
            "frame_count": str(veh.frame_count),
            "zone": zone_name,
            "alert_level": alert_level,
            "alert_triggered": str(alert_triggered),
            "vehicle_class": veh.class_name,
            "vehicle_confidence": str(round(veh.confidence, 3)),
            "vehicle_id": veh.vehicle_id,
            "vehicle_first_seen": str(int(veh.first_seen)),
            "snapshot_key": veh.snapshot_key,
            "snapshot_bbox": json.dumps(veh.snapshot_bbox),
            "time_period": get_time_period(),
        }

        self.r.xadd(EVENT_STREAM, event, maxlen=MAX_EVENT_STREAM_LEN)
        self.total_events += 1

        logger.info(
            f"EVENT: vehicle_left | {veh.class_name} after {visit_duration:.1f}s"
            f"{f' | zone={zone_name}' if zone_name else ''}"
        )

    def _emit_vehicle_gone_event(self, veh: 'TrackedVehicle', timestamp: float):
        """Emit a vehicle_gone event when a tracked vehicle's ghost expires.

        Fires for EVERY track end — both drive-bys and idle-leaves. Used by
        `vehicle-attributes-cam{N}` to flush its per-track HD-crop buffer
        regardless of whether the vehicle ever went idle. Carries `was_idle`
        so consumers can distinguish without re-deriving from `duration`.

        Drive-by tracks (short duration, never set idle_alerted): only
        vehicle_gone fires. Idle-leave tracks: BOTH vehicle_gone (internal)
        AND vehicle_left (user-facing) fire.
        """
        visit_duration = veh.last_seen - veh.first_seen

        event = {
            "camera_id": CAMERA_ID,
            "event_type": VEHICLE_GONE_EVENT,
            "timestamp": str(timestamp),
            "duration": str(round(visit_duration, 1)),
            "bbox": json.dumps(veh.bbox),
            "frame_count": str(veh.frame_count),
            "vehicle_class": veh.class_name,
            "vehicle_confidence": str(round(veh.confidence, 3)),
            "vehicle_id": veh.vehicle_id,
            "vehicle_first_seen": str(int(veh.first_seen)),
            "was_idle": str(bool(veh.idle_alerted)),
        }

        self.r.xadd(EVENT_STREAM, event, maxlen=MAX_EVENT_STREAM_LEN)
        self.total_events += 1

    def _load_zones(self):
        """Load zone definitions from Redis (cached)."""
        now = time.time()
        if now - self._zone_load_time < self._zone_reload_interval:
            return

        try:
            raw = self.r.hgetall(ZONE_KEY)
            self._zones = {}
            for k, v in raw.items():
                key = k.decode() if isinstance(k, bytes) else k
                val = v.decode() if isinstance(v, bytes) else v
                self._zones[key] = json.loads(val)
        except Exception as e:
            logger.debug(f"Zone load error: {e}")

        self._zone_load_time = now

    def _find_zone(self, bbox: list) -> tuple:
        """
        Check which zone a person's bbox center falls in.

        Returns (zone_name, alert_level) or ("", "") if no zone.
        """
        self._load_zones()

        if not self._zones or len(bbox) != 4:
            return ("", "")

        # Normalize bbox center to 0-1 using actual frame dimensions
        frame_w = self.frame_width
        frame_h = self.frame_height

        cx = ((bbox[0] + bbox[2]) / 2) / frame_w
        cy = ((bbox[1] + bbox[3]) / 2) / frame_h

        for zone_id, zone in self._zones.items():
            pts = zone.get("points", [])
            if len(pts) >= 3 and point_in_polygon(cx, cy, pts):
                return (zone.get("name", zone_id), zone.get("alert_level", "log_only"))

        return ("", "")

    def _check_in_dead_zone(self, bbox: list) -> bool:
        """Return True if the bbox center falls in a 'dead_zone' — fully ignored area."""
        self._load_zones()
        if not self._zones or len(bbox) != 4:
            return False
        frame_w = self.frame_width
        frame_h = self.frame_height
        cx = ((bbox[0] + bbox[2]) / 2) / frame_w
        cy = ((bbox[1] + bbox[3]) / 2) / frame_h
        for zone_id, zone in self._zones.items():
            if zone.get("alert_level", "") != "dead_zone":
                continue
            pts = zone.get("points", [])
            if len(pts) >= 3 and point_in_polygon(cx, cy, pts):
                return True
        return False

    def _update_identities(self):
        """Read face identity state from Redis and map names to tracked persons."""
        now = time.time()
        if now - self._identity_load_time < 2:  # Check every 2 seconds
            return
        self._identity_load_time = now

        try:
            id_state = self.r.hgetall(IDENTITY_KEY)
            if not id_state:
                return
            id_json = id_state.get(b"identities", id_state.get("identities", b"[]"))
            if isinstance(id_json, bytes):
                id_json = id_json.decode()
            identities = json.loads(id_json)
        except Exception:
            return

        for ident in identities:
            id_name = ident.get("name", "Unknown")
            if id_name == "Unknown":
                continue
            id_bbox = ident.get("bbox", [])
            if len(id_bbox) != 4:
                continue
            # Skip identities whose face bbox sits inside a dead zone —
            # don't let an identity match in a "don't care" area assign
            # a name to a legitimate person whose bbox happens to overlap.
            if self._check_in_dead_zone(id_bbox):
                continue
            # Match identity bbox to a tracked person via IoU
            best_iou = 0.0
            best_person = None
            for person in self.tracked.values():
                iou = compute_iou(id_bbox, person.bbox)
                if iou > best_iou:
                    best_iou = iou
                    best_person = person
            if best_iou > 0.2 and best_person:
                if not best_person.identity_name:
                    # First identification — emit event
                    best_person.identity_name = id_name
                    best_person._pending_identity = id_name
                    best_person._pending_identity_count = 1
                    self._emit_event(
                        "person_identified", best_person, now,
                        extra={"identity_name": id_name}
                    )
                elif id_name == best_person.identity_name:
                    # Same name — clear any pending flip candidate.
                    best_person._pending_identity = id_name
                    best_person._pending_identity_count = 0
                else:
                    # Different name proposed for an already-identified
                    # person. Require N consecutive cycles agreeing on
                    # the new name before overwriting; one bad face
                    # frame shouldn't corrupt the track. Always log so
                    # unexpected flips show up in operator review.
                    if best_person._pending_identity == id_name:
                        best_person._pending_identity_count += 1
                    else:
                        best_person._pending_identity = id_name
                        best_person._pending_identity_count = 1
                    logger.info(
                        f"Identity flip candidate: {best_person.person_id} "
                        f"'{best_person.identity_name}' → '{id_name}' "
                        f"({best_person._pending_identity_count}"
                        f"/{TrackedPerson._IDENTITY_FLIP_CONFIRM_CYCLES})"
                    )
                    if (best_person._pending_identity_count
                            >= TrackedPerson._IDENTITY_FLIP_CONFIRM_CYCLES):
                        previous = best_person.identity_name
                        logger.warning(
                            f"Identity flip CONFIRMED: {best_person.person_id} "
                            f"'{previous}' → '{id_name}'"
                        )
                        best_person.identity_name = id_name
                        best_person._pending_identity_count = 0
                        self._emit_event(
                            "person_identified", best_person, now,
                            extra={
                                "identity_name": id_name,
                                "previous_identity": previous,
                            },
                        )

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

            if best_iou >= self.iou_threshold and best_track_id not in matched_track_ids:
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
        lost_ids = []
        for track_id, person in self.tracked.items():
            time_since_seen = current_time - person.last_seen
            if time_since_seen > self.lost_timeout:
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
