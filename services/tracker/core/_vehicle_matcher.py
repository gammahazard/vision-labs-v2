"""tracker/core/_vehicle_matcher.py — VehicleMatcherMixin.

Extracted from manager.py during the 2026-05-22 mixin split.

Mixed into PersonTracker. Owns the four fallback match strategies the
primary IoU loop in `_process_vehicle_detections` falls through to —
idle-IoM rescue, idle-IoM ghost rescue, live center-distance, ghost
center-distance — plus the bbox-area + occlusion helpers used to gate
vehicle_sample emit. The orchestrator method
`_process_vehicle_detections` itself stays in manager.py because its
behavior depends on module-level env-var constants there (and tests
rely on `importlib.reload(manager)` re-reading those env vars).
"""

from ._classes import _class_compatible
from .config import (
    VEHICLE_IDLE_IOM_THRESHOLD,
    VEHICLE_IDLE_IOM_AREA_RATIO_MAX,
    VEHICLE_MATCH_STALE_SECS,
    VEHICLE_MATCH_AREA_RATIO_MAX,
    VEHICLE_CENTER_MATCH_STALE_SECS,
    SAMPLE_OCCLUSION_IOU_THRESHOLD,
    VEHICLE_GHOST_MAX_DIST_RATIO,
)
from .iou import compute_iou
from .state import TrackedVehicle  # noqa: F401  (used in forward-ref type hint)


class VehicleMatcherMixin:
    """Fallback vehicle-match strategies + sample-quality gates."""

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

    def _try_idle_iom_ghost_match(self, bbox: list, class_name: str) -> str | None:
        """Same as _try_idle_iom_match but checks _ghost_vehicles (idle only).

        Needed because when a parked car's detector intermittently drops
        for >VEHICLE_LOST_TIMEOUT (10 s) — e.g., a passing vehicle visually
        blocks YOLO for a moment — the idle track moves to the ghost
        buffer. Without this rescue, a non-idle live track passing through
        the same area can claim the parked car's subsequent detections via
        the primary IoU loop before the regular ghost_match (center
        distance) gets a chance, because primary IoU runs first.

        Returns a ghost vehicle_id when the same gates pass
        (IoM ≥ VEHICLE_IDLE_IOM_THRESHOLD, area ratio ≤ ...AREA_RATIO_MAX,
        class compatible). Caller must pop from _ghost_vehicles and add
        back to tracked_vehicles (mirroring _try_ghost_match's contract).
        """
        a_x1, a_y1, a_x2, a_y2 = bbox
        a_area = (a_x2 - a_x1) * (a_y2 - a_y1)
        if a_area <= 0:
            return None

        best_vid = None
        best_iom = VEHICLE_IDLE_IOM_THRESHOLD

        for vid, (veh, _ghost_ts) in self._ghost_vehicles.items():
            if not veh.idle_alerted:
                continue  # only idle ghosts are sticky
            if not _class_compatible(veh.class_name, class_name):
                continue
            b_x1, b_y1, b_x2, b_y2 = veh.bbox
            b_area = (b_x2 - b_x1) * (b_y2 - b_y1)
            if b_area <= 0:
                continue
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

    @staticmethod
    def _bbox_area(bbox: list) -> int:
        """Sub-stream-coordinate bbox area (used as a sample-quality
        gate — see MIN_SAMPLE_BBOX_AREA_SUB_PX). Returns 0 for a
        degenerate / empty bbox."""
        if not bbox or len(bbox) < 4:
            return 0
        w = max(0, bbox[2] - bbox[0])
        h = max(0, bbox[3] - bbox[1])
        return int(w * h)

    def _sample_occluded_by_moving_vehicle(self, veh: 'TrackedVehicle') -> bool:
        """True if a vehicle_sample for `veh` would capture pixels of
        another foreground (moving) vehicle.

        Returns True when any OTHER currently-tracked vehicle that is NOT
        idle_alerted and NOT is_stationary has bbox IoU above
        SAMPLE_OCCLUSION_IOU_THRESHOLD with `veh`. The idle-vs-idle carve-out
        keeps two adjacent permanently-parked cars from blocking each
        other's sampling.

        Used to skip vehicle_sample emit during drive-by occlusion of a
        parked car (the parked track's bbox region in the HD frame
        contains pixels of the foreground intruder, polluting the
        classifier's color/body/make/model vote).
        """
        for other_vid, other_veh in self.tracked_vehicles.items():
            if other_vid == veh.vehicle_id:
                continue
            if other_veh.idle_alerted or other_veh.is_stationary:
                continue
            if compute_iou(veh.bbox, other_veh.bbox) > SAMPLE_OCCLUSION_IOU_THRESHOLD:
                return True
        return False

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
        det_area = max(0, bbox[2] - bbox[0]) * max(0, bbox[3] - bbox[1])
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
            # Stale-track skip: this fallback exists to recover the fast-
            # mover IoU-jitter case (~200 ms drift between consecutive
            # frames). For a stale track (>VEHICLE_CENTER_MATCH_STALE_SECS
            # since last_seen), the 3.5×bbox_w radius is a vast region —
            # easily grabbing a completely different physical vehicle that
            # happens to enter the same screen area. Hard-skip rather than
            # scale the radius: simpler, and stale tracks should naturally
            # ghost out anyway.
            if current_ts and (current_ts - veh.last_seen) > VEHICLE_CENTER_MATCH_STALE_SECS:
                continue
            if not _class_compatible(veh.class_name, class_name):
                continue  # bus vs car etc; car↔truck flicker is allowed
            # Size-ratio gate for stale tracks. Mirrors the primary IoU
            # loop's gate — without it, a non-idle stale track whose
            # bbox center happens to be near a new (much-larger or
            # much-smaller) detection would still merge via this loose
            # center-distance fallback. Same live-regression as the
            # primary loop's gate (school bus inheriting the small car's
            # track at 15:09:45).
            if current_ts and (current_ts - veh.last_seen) > VEHICLE_MATCH_STALE_SECS:
                veh_area = max(0, veh.bbox[2] - veh.bbox[0]) * max(0, veh.bbox[3] - veh.bbox[1])
                if det_area > 0 and veh_area > 0:
                    area_ratio = max(det_area, veh_area) / min(det_area, veh_area)
                    if area_ratio > VEHICLE_MATCH_AREA_RATIO_MAX:
                        continue
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
