"""tracker/core/state.py — TrackedVehicle and TrackedPerson dataclasses.

Both classes are state holders, not orchestrators — they carry per-entity
history and expose computed properties (duration, direction, is_stationary)
that PersonTracker (manager.py) consumes.
"""

from .config import (
    classify_action,
    ACTION_DEBOUNCE_FRAMES,
    ACTION_STICKY_MULTIPLIER,
)

class TrackedVehicle:
    """
    Represents a vehicle being tracked across frames.

    Uses IoU matching to determine if a detected vehicle is the same one
    seen in previous frames. Tracks duration for idle detection.
    """

    # Class label changes are noisy frame-to-frame (truck flips to car
    # when partly occluded). We keep a short history and report the
    # mode so the event stamp matches what the vehicle MOSTLY looks like.
    _CLASS_HISTORY_LEN = 10

    def __init__(self, vehicle_id: str, bbox: list, class_name: str,
                 confidence: float, timestamp: float):
        self.vehicle_id = vehicle_id
        self.bbox = bbox                    # Current bounding box [x1, y1, x2, y2]
        self.class_name = class_name        # car, truck, bus, motorcycle, bicycle
        self._class_history: list[str] = [class_name]
        self.confidence = confidence
        self.first_seen = timestamp         # When this vehicle first appeared
        self.last_seen = timestamp          # Last frame it was detected in
        self.frame_count = 1
        self.idle_alerted = False           # Has idle notification fired
        self.snapshot_key = ""              # Redis key for stored snapshot
        self.snapshot_bbox = bbox           # Bbox at the time snapshot was captured
        # Track center positions for movement detection
        cx = (bbox[0] + bbox[2]) / 2
        cy = (bbox[1] + bbox[3]) / 2
        self.center_history: list[tuple] = [(cx, cy)]

    def update(self, bbox: list, class_name: str, confidence: float,
               timestamp: float):
        """Update vehicle state with a new detection."""
        self.bbox = bbox
        self.confidence = confidence
        self.last_seen = timestamp
        self.frame_count += 1
        # Track center for movement detection (keep last 20)
        cx = (bbox[0] + bbox[2]) / 2
        cy = (bbox[1] + bbox[3]) / 2
        self.center_history.append((cx, cy))
        if len(self.center_history) > 20:
            self.center_history.pop(0)
        # Update class history → mode. Stabilises against noisy
        # truck↔car flips on partial occlusion.
        self._class_history.append(class_name)
        if len(self._class_history) > self._CLASS_HISTORY_LEN:
            self._class_history.pop(0)
        # Mode (ties broken by most-recent label, which is
        # what max() returns for ties on `count` due to stable iter).
        counts: dict[str, int] = {}
        for c in self._class_history:
            counts[c] = counts.get(c, 0) + 1
        self.class_name = max(counts, key=counts.get)
        # If the vehicle visibly moves again, allow a fresh idle alert
        # the next time it re-parks. Without this, a parked car that
        # drives off + comes back would never emit a second vehicle_idle.
        if self.idle_alerted and not self.is_stationary:
            self.idle_alerted = False

    @property
    def duration(self) -> float:
        """How long this vehicle has been seen (seconds)."""
        return self.last_seen - self.first_seen

    @property
    def is_stationary(self) -> bool:
        """Check if the vehicle has stayed in roughly the same spot.

        Compares the CURRENT center against the MEDIAN of the rolling
        20-sample history (~4s at 5 FPS). Threshold scales with bbox
        width (10%, min 8 px) so the test works at any distance.

        Why median, not first-sample: YOLO bbox jitter on a parked car
        regularly produces 3-5 px shifts frame-to-frame even when the
        car hasn't moved. Comparing against the oldest sample treats
        that jitter as cumulative drift; against the median it averages
        out. Also resists noisy outliers (single misdetection bbox).
        """
        if len(self.center_history) < 5:
            return False  # Not enough samples yet
        xs = sorted(c[0] for c in self.center_history)
        ys = sorted(c[1] for c in self.center_history)
        mid = len(xs) // 2
        ref_cx, ref_cy = xs[mid], ys[mid]
        cur_cx, cur_cy = self.center_history[-1]
        drift = ((cur_cx - ref_cx) ** 2 + (cur_cy - ref_cy) ** 2) ** 0.5
        bbox_w = max(1.0, self.bbox[2] - self.bbox[0])
        threshold = max(8.0, bbox_w * 0.10)
        return drift < threshold


class TrackedPerson:
    """
    Represents a person being tracked across frames.

    Stores their current bounding box, when they first appeared,
    when they were last seen, and movement history for direction estimation.
    """

    # Identity-flip protection: when a NEW face-rec name comes in for a
    # person who already has a sticky identity, require this many
    # consecutive identity-load cycles agreeing on the new name before
    # actually overwriting. Stops a single bad face frame from corrupting
    # the track. Logs every observed flip attempt at INFO so unexpected
    # behaviour shows up in the logs without silencing the data.
    _IDENTITY_FLIP_CONFIRM_CYCLES = 3

    # Per-person cooldown on action_changed events. Prevents spam when a
    # pose oscillates around the debounce/sticky thresholds.
    _ACTION_EVENT_COOLDOWN_SEC = 2.0

    def __init__(self, person_id: str, bbox: list, timestamp: float):
        self.person_id = person_id
        self.bbox = bbox                    # Current bounding box [x1, y1, x2, y2]
        self.first_seen = timestamp         # When this person first appeared
        self.last_seen = timestamp          # Last frame they were detected in
        self.frame_count = 1               # How many frames they've been in
        self.bbox_history: list[list] = [bbox]  # For direction estimation
        self.announced = False              # Whether we've emitted "person_appeared"
        self.announce_after = None          # Timestamp when grace period expires (if deferred)
        self.action = "unknown"            # Current (stable) action
        self.action_confidence = 0.0       # How confident in the action classification
        self._pending_action = "unknown"   # Candidate action being debounced
        self._pending_count = 0            # Consecutive frames with pending action
        self._last_action_event_ts = 0.0   # Last action_changed emit time (cooldown)
        self.identity_name = ""            # Name from face recognition (sticky)
        # Identity-flip debounce state. _pending_identity is the candidate
        # name we're observing; _pending_identity_count is how many
        # consecutive cycles it has appeared. The current sticky identity
        # is in `identity_name` above.
        self._pending_identity = ""
        self._pending_identity_count = 0

    def update(self, bbox: list, timestamp: float, keypoints: list = None):
        """Update this person's state with a new detection. Returns previous action."""
        self.bbox = bbox
        self.last_seen = timestamp
        self.frame_count += 1
        # Keep last 10 positions for direction estimation
        self.bbox_history.append(bbox)
        if len(self.bbox_history) > 10:
            self.bbox_history.pop(0)
        # Classify action from keypoints (with debounce + sticky bias).
        # Pass bbox so the classifier can scale its pixel thresholds —
        # otherwise distant/small detections fail the absolute-pixel checks.
        prev_action = self.action
        if keypoints:
            result = classify_action(keypoints, bbox=bbox)
            raw_action = result["action"]
            # Debounce: only change if new action is stable for N consecutive frames
            if raw_action == self._pending_action:
                self._pending_count += 1
            else:
                self._pending_action = raw_action
                self._pending_count = 1
            # Sticky bias: once we have a real action, require more evidence to change
            threshold = ACTION_DEBOUNCE_FRAMES
            if self.action not in ("unknown", ""):
                threshold = ACTION_DEBOUNCE_FRAMES * ACTION_STICKY_MULTIPLIER
            if self._pending_count >= threshold:
                self.action = raw_action
                self.action_confidence = result["confidence"]
        return prev_action

    @property
    def duration(self) -> float:
        """How long this person has been visible (seconds)."""
        return self.last_seen - self.first_seen

    @property
    def center(self) -> tuple[float, float]:
        """Center point of the current bounding box."""
        return (
            (self.bbox[0] + self.bbox[2]) / 2,
            (self.bbox[1] + self.bbox[3]) / 2,
        )

    @property
    def direction(self) -> str:
        """
        Estimate movement direction based on bbox center history.

        Uses the mean of the first half vs the mean of the second half
        of `bbox_history`, instead of comparing single endpoints — one
        jittery sample at either end used to flip the answer between
        frames. Threshold scales with the current bbox width so a tiny
        wobble for a small/distant person isn't reported as motion.

        Returns: "left", "right", "stationary", or "unknown"
        """
        n = len(self.bbox_history)
        if n < 4:
            return "unknown"

        half = n // 2
        first_xs = [
            (b[0] + b[2]) / 2 for b in self.bbox_history[:half]
        ]
        last_xs = [
            (b[0] + b[2]) / 2 for b in self.bbox_history[-half:]
        ]
        dx = (sum(last_xs) / len(last_xs)) - (sum(first_xs) / len(first_xs))

        # Pixel threshold scaled by bbox width (min 8 px) so the
        # classifier works at any frame size / distance.
        bbox_w = max(1.0, self.bbox[2] - self.bbox[0])
        threshold = max(8.0, bbox_w * 0.10)

        if abs(dx) < threshold:
            return "stationary"
        elif dx > 0:
            return "right"
        else:
            return "left"

    def to_dict(self) -> dict:
        """Serialize for Redis state snapshot."""
        return {
            "person_id": self.person_id,
            "bbox": self.bbox,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "duration": round(self.duration, 1),
            "direction": self.direction,
            "action": self.action,
            "frame_count": self.frame_count,
            "identity_name": self.identity_name,
        }
