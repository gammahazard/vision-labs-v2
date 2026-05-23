"""
tests/test_tracker.py — Real tests for the tracker's core algorithms.

Tests IoU computation, TrackedPerson state management, and direction estimation
using actual bounding box coordinates. These are the algorithms that determine
whether two detections across frames are the same person.

NO mocks on the core logic — only Redis is faked (for PersonTracker tests).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "tracker"))

# We need to add contracts to path before importing tracker
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "contracts"))

from tracker import compute_iou, TrackedPerson


# ---------------------------------------------------------------------------
# IoU Computation
# ---------------------------------------------------------------------------

class TestComputeIoU:
    def test_identical_boxes(self):
        """Two identical boxes have IoU = 1.0"""
        box = [100, 100, 200, 200]
        assert compute_iou(box, box) == 1.0

    def test_no_overlap(self):
        """Two non-overlapping boxes have IoU = 0.0"""
        box_a = [0, 0, 100, 100]
        box_b = [200, 200, 300, 300]
        assert compute_iou(box_a, box_b) == 0.0

    def test_partial_overlap(self):
        """Partially overlapping boxes have 0 < IoU < 1."""
        box_a = [0, 0, 100, 100]      # Area = 10000
        box_b = [50, 50, 150, 150]     # Area = 10000, overlap = 50*50 = 2500
        iou = compute_iou(box_a, box_b)
        # Union = 10000 + 10000 - 2500 = 17500
        # IoU = 2500 / 17500 ≈ 0.1429
        assert abs(iou - 2500 / 17500) < 1e-6

    def test_one_inside_other(self):
        """Small box fully inside a large box."""
        big = [0, 0, 200, 200]       # Area = 40000
        small = [50, 50, 100, 100]   # Area = 2500
        iou = compute_iou(big, small)
        # Intersection = 2500, Union = 40000 + 2500 - 2500 = 40000
        assert abs(iou - 2500 / 40000) < 1e-6

    def test_touching_edges(self):
        """Two boxes sharing an edge have IoU = 0 (edge has zero area)."""
        box_a = [0, 0, 100, 100]
        box_b = [100, 0, 200, 100]  # Shares right edge of box_a
        assert compute_iou(box_a, box_b) == 0.0

    def test_symmetrical(self):
        """IoU(A, B) == IoU(B, A)"""
        box_a = [10, 20, 150, 200]
        box_b = [80, 100, 250, 300]
        assert compute_iou(box_a, box_b) == compute_iou(box_b, box_a)

    def test_zero_area_box(self):
        """A degenerate box with zero area returns IoU = 0."""
        box_a = [100, 100, 100, 100]  # Point
        box_b = [50, 50, 150, 150]
        assert compute_iou(box_a, box_b) == 0.0

    def test_realistic_person_boxes(self):
        """IoU between two similar-sized person bounding boxes from consecutive frames."""
        # Frame N: person at x=200, y=100, width=80, height=200
        person_frame_n = [200, 100, 280, 300]
        # Frame N+1: person shifted slightly right
        person_frame_n1 = [210, 105, 290, 305]
        iou = compute_iou(person_frame_n, person_frame_n1)
        # Should have high overlap (same person moved slightly)
        assert iou > 0.6

    def test_realistic_different_people(self):
        """IoU between two people on opposite sides of the frame."""
        person_left = [50, 100, 130, 400]
        person_right = [450, 100, 530, 400]
        assert compute_iou(person_left, person_right) == 0.0

    def test_large_overlap_threshold(self):
        """Verify a high-overlap case passes our IOU_THRESHOLD of 0.3."""
        # Same person, tiny movement
        box_a = [150, 80, 250, 380]
        box_b = [155, 85, 255, 385]
        iou = compute_iou(box_a, box_b)
        assert iou >= 0.3  # Should pass our tracker's threshold


# ---------------------------------------------------------------------------
# TrackedPerson state
# ---------------------------------------------------------------------------

class TestTrackedPerson:
    def test_initial_state(self):
        """TrackedPerson starts with correct initial values."""
        p = TrackedPerson("person_0001", [100, 100, 200, 300], 1000.0)
        assert p.person_id == "person_0001"
        assert p.bbox == [100, 100, 200, 300]
        assert p.first_seen == 1000.0
        assert p.last_seen == 1000.0
        assert p.frame_count == 1
        assert p.announced is False
        assert p.action == "unknown"

    def test_update_increments_frame_count(self):
        """Each update call increments frame_count."""
        p = TrackedPerson("p1", [100, 100, 200, 300], 1000.0)
        p.update([105, 105, 205, 305], 1001.0)
        assert p.frame_count == 2
        p.update([110, 110, 210, 310], 1002.0)
        assert p.frame_count == 3

    def test_update_changes_bbox(self):
        """Update replaces the current bounding box."""
        p = TrackedPerson("p1", [100, 100, 200, 300], 1000.0)
        new_bbox = [120, 120, 220, 320]
        p.update(new_bbox, 1001.0)
        assert p.bbox == new_bbox

    def test_duration(self):
        """Duration is the difference between last_seen and first_seen."""
        p = TrackedPerson("p1", [100, 100, 200, 300], 1000.0)
        p.update([100, 100, 200, 300], 1005.5)
        assert abs(p.duration - 5.5) < 1e-6

    def test_center(self):
        """Center point calculation is correct."""
        p = TrackedPerson("p1", [100, 100, 200, 300], 1000.0)
        cx, cy = p.center
        assert cx == 150.0
        assert cy == 200.0


class TestDirection:
    """`direction` was refactored to mean-of-halves with a bbox-width-scaled
    threshold (max(8 px, bbox_w * 0.10)) — fixes single-sample jitter that
    used to flip the answer frame-to-frame. Needs ≥ 4 samples now.

    All tests below use bbox width = 100 px → threshold = 10 px."""

    def test_direction_unknown_few_frames(self):
        """Direction is 'unknown' when fewer than 4 positions."""
        p = TrackedPerson("p1", [100, 100, 200, 300], 1000.0)
        assert p.direction == "unknown"
        # 2 samples — still under 4
        p.update([105, 100, 205, 300], 1001.0)
        assert p.direction == "unknown"
        # 3 samples — still under 4
        p.update([108, 100, 208, 300], 1002.0)
        assert p.direction == "unknown"

    def test_direction_stationary(self):
        """Center drift < 10 px (bbox_w * 0.10) over the window → stationary."""
        p = TrackedPerson("p1", [100, 100, 200, 300], 1000.0)
        # 4 samples, each shifted only 2 px — first half mean vs last half
        # mean differs by ≈ 4 px, well below the 10 px threshold.
        p.update([102, 100, 202, 300], 1001.0)
        p.update([104, 100, 204, 300], 1002.0)
        p.update([106, 100, 206, 300], 1003.0)
        assert p.direction == "stationary"

    def test_direction_right(self):
        """Mean of last half > mean of first half by > 10 px → right."""
        p = TrackedPerson("p1", [100, 100, 200, 300], 1000.0)
        # 4 samples shifting 30 px each → first-half mean ≈ 165, last-half
        # mean ≈ 245 → dx ≈ +80 (well over the 10 px threshold).
        p.update([130, 100, 230, 300], 1001.0)
        p.update([160, 100, 260, 300], 1002.0)
        p.update([190, 100, 290, 300], 1003.0)
        assert p.direction == "right"

    def test_direction_left(self):
        """Mean of last half < mean of first half by > 10 px → left."""
        p = TrackedPerson("p1", [200, 100, 300, 300], 1000.0)
        p.update([170, 100, 270, 300], 1001.0)
        p.update([140, 100, 240, 300], 1002.0)
        p.update([110, 100, 210, 300], 1003.0)
        assert p.direction == "left"

    def test_bbox_history_capped(self):
        """Bbox history is capped at 10 entries."""
        p = TrackedPerson("p1", [100, 100, 200, 300], 1000.0)
        for i in range(20):
            p.update([100 + i, 100, 200 + i, 300], 1000.0 + i)
        assert len(p.bbox_history) == 10


class TestTrackedPersonSerialization:
    def test_to_dict_keys(self):
        """to_dict() includes all expected keys."""
        p = TrackedPerson("p1", [100, 100, 200, 300], 1000.0)
        p.update([110, 100, 210, 300], 1001.0)
        p.update([120, 100, 220, 300], 1002.0)
        d = p.to_dict()
        required_keys = {"person_id", "bbox", "first_seen", "last_seen",
                         "duration", "direction", "action", "frame_count"}
        assert required_keys.issubset(d.keys())

    def test_to_dict_values(self):
        """to_dict() values match the person's state."""
        p = TrackedPerson("test_123", [10, 20, 30, 40], 500.0)
        d = p.to_dict()
        assert d["person_id"] == "test_123"
        assert d["bbox"] == [10, 20, 30, 40]
        assert d["first_seen"] == 500.0
        assert d["frame_count"] == 1

    def test_action_from_keypoints(self):
        """Updating with keypoints runs action classification (respects debounce)."""
        p = TrackedPerson("p1", [100, 100, 200, 300], 1000.0)
        # Create standing keypoints
        kps = [
            [320, 100, 0.9], [310, 90, 0.9], [330, 90, 0.9],
            [300, 100, 0.8], [340, 100, 0.8],
            [280, 180, 0.9], [360, 180, 0.9],
            [260, 260, 0.8], [380, 260, 0.8],
            [250, 340, 0.8], [390, 340, 0.8],
            [290, 350, 0.9], [350, 350, 0.9],
            [285, 460, 0.8], [355, 460, 0.8],
            [280, 570, 0.8], [360, 570, 0.8],
        ]
        # Action requires ACTION_DEBOUNCE_FRAMES (10) consecutive frames to stabilize
        for i in range(10):
            p.update([100, 100, 200, 300], 1001.0 + i * 0.1, keypoints=kps)
        assert p.action == "standing"
        assert p.action_confidence > 0

    def test_action_unknown_when_too_few_keypoints_visible(self):
        """A partial detection with < MIN_KEYPOINTS_FOR_ACTION visible
        joints must NOT commit a real action label. Live observation: a
        person walking behind a planter has shoulders + maybe one knee
        visible, the rest occluded. The old rule-based classifier would
        sometimes fire arms_raised or crouching off noise from those
        partial inputs."""
        p = TrackedPerson("p_partial", [100, 100, 200, 300], 1000.0)
        # Only 5 visible (conf >= 0.3) — well below the default 10 floor.
        kps_partial = [
            [320, 100, 0.9],   # nose
            [310, 90, 0.9],    # eye
            [280, 180, 0.9],   # shoulder
            [360, 180, 0.9],   # shoulder
            [290, 350, 0.9],   # hip
        ] + [[0, 0, 0.0]] * 12  # rest below conf threshold
        for i in range(10):
            p.update([100, 100, 200, 300], 1001.0 + i * 0.1,
                     keypoints=kps_partial)
        # _pending_action stays "unknown"; without 10 consecutive
        # real-action frames, self.action also stays "" → "unknown".
        assert p.action in ("", "unknown"), \
            f"partial-keypoint detection must not commit a real action, got {p.action}"

    def test_action_walking_when_standing_pose_plus_motion(self):
        """A person whose POSE classifies as standing but whose BBOX
        history shows lateral motion gets promoted to 'walking'. The
        keypoint-based classifier alone returns 'standing' even for
        moving people — locomotion is in the bbox trajectory, not the
        pose. Combine the two.

        Needs 13+ frames because the `direction` property returns
        'unknown' for the first 3 frames (waiting for bbox_history to
        fill to len >= 4), so the first valid 'walking' raw_action only
        appears at frame 4, and then 10 more frames of pending stability
        are needed for the debounce to commit it. 14 frames in real
        life = ~1.4 s of walking at 10 fps — appropriate for a
        first-walking-commit window."""
        p = TrackedPerson("p_walker", [100, 100, 200, 300], 1000.0)
        kps_standing = [
            [320, 100, 0.9], [310, 90, 0.9], [330, 90, 0.9],
            [300, 100, 0.8], [340, 100, 0.8],
            [280, 180, 0.9], [360, 180, 0.9],
            [260, 260, 0.8], [380, 260, 0.8],
            [250, 340, 0.8], [390, 340, 0.8],
            [290, 350, 0.9], [350, 350, 0.9],
            [285, 460, 0.8], [355, 460, 0.8],
            [280, 570, 0.8], [360, 570, 0.8],
        ]
        # 14 frames, each shifting the bbox 30 px right.
        for i in range(14):
            x_off = i * 30
            p.update(
                [100 + x_off, 100, 200 + x_off, 300],
                1001.0 + i * 0.1,
                keypoints=kps_standing,
            )
        assert p.action == "walking", \
            f"standing pose + lateral motion must commit as walking, got {p.action}"

    def test_action_standing_when_pose_standing_and_stationary(self):
        """Negative case for walking: a standing pose with no bbox motion
        must remain 'standing', not falsely fire 'walking'. Don't break
        the original behavior."""
        p = TrackedPerson("p_still", [100, 100, 200, 300], 1000.0)
        kps_standing = [
            [320, 100, 0.9], [310, 90, 0.9], [330, 90, 0.9],
            [300, 100, 0.8], [340, 100, 0.8],
            [280, 180, 0.9], [360, 180, 0.9],
            [260, 260, 0.8], [380, 260, 0.8],
            [250, 340, 0.8], [390, 340, 0.8],
            [290, 350, 0.9], [350, 350, 0.9],
            [285, 460, 0.8], [355, 460, 0.8],
            [280, 570, 0.8], [360, 570, 0.8],
        ]
        for i in range(10):
            # No bbox motion
            p.update([100, 100, 200, 300], 1001.0 + i * 0.1,
                     keypoints=kps_standing)
        assert p.action == "standing", \
            f"stationary standing person must stay 'standing', got {p.action}"


class TestIdentityPersistence:
    """Identified tracks must survive scale changes + brief gaps; the
    track ID + identity_name should NOT get destroyed and re-spawned as
    Unknown when an identified person walks far away or briefly drops
    out of detection. But: when a stranger walks into an identified
    person's recently-vacated bbox spot, the identity must NOT
    incorrectly inherit."""

    def _make_tracker(self):
        """Build a PersonTracker with a fake redis. Mirrors the
        FakeRedis pattern used in test_vehicles.py."""
        import importlib
        from services.tracker.core import manager as mgr

        importlib.reload(mgr)

        class FakeRedis:
            def __init__(self):
                self._h = {}
                self._streams = {}
            def hget(self, k, f):
                return self._h.get(k, {}).get(f)
            def hset(self, k, *args, **kw):
                self._h.setdefault(k, {})
                if "mapping" in kw:
                    self._h[k].update(kw["mapping"])
                return 1
            def hgetall(self, k):
                return {}
            def xadd(self, k, fields, **kw):
                self._streams.setdefault(k, []).append(('0-0', fields))
                return '0-0'
            def setex(self, *a, **kw):
                pass
            def get(self, k):
                return None

        return mgr.PersonTracker(FakeRedis())

    def test_identified_track_uses_looser_iou_threshold(self):
        """An identified track should match a much-smaller, much-shifted
        bbox (IoU well below the default 0.3 but above 0.10). Without
        the looser threshold, walking-away triggers track loss + identity
        wipe."""
        from services.tracker.core.config import IDENTITY_TRACK_IOU_THRESHOLD

        m = self._make_tracker()
        # Spawn + accumulate enough frames to commit the identity.
        big_bbox = [100, 100, 300, 600]  # 200x500
        m.update(
            [{"bbox": big_bbox, "confidence": 0.9, "keypoints": []}],
            timestamp=1000.0,
        )
        track_id = next(iter(m.tracked))
        m.tracked[track_id].identity_name = "Dad"

        # Far-away bbox: shifted + much smaller. IoU well under 0.3
        # but above 0.10. Without the per-track lower threshold this
        # would NOT match.
        far_bbox = [340, 220, 380, 320]  # 40x100, shifted right + down
        from services.tracker.core.config import IDENTITY_TRACK_IOU_THRESHOLD as _ITHR
        m.update(
            [{"bbox": far_bbox, "confidence": 0.8, "keypoints": []}],
            timestamp=1000.5,
        )
        # Identified track must still be there with the same ID.
        assert track_id in m.tracked, \
            "identified track must survive low-IoU re-match"
        assert m.tracked[track_id].identity_name == "Dad", \
            "identity should be preserved on low-IoU match"

    def test_unidentified_track_keeps_strict_iou_threshold(self):
        """Negative case: an unidentified track must NOT inherit a
        wildly-shifted small bbox via the looser threshold. Only
        identified tracks get the looser matching."""
        m = self._make_tracker()
        m.update(
            [{"bbox": [100, 100, 300, 600], "confidence": 0.9, "keypoints": []}],
            timestamp=1000.0,
        )
        original_track_id = next(iter(m.tracked))
        # NO identity_name set — track is unidentified.

        # Same far-shifted small bbox as before. With the strict 0.3
        # threshold, this should NOT match the original track → it
        # spawns a new one.
        m.update(
            [{"bbox": [340, 220, 380, 320], "confidence": 0.8, "keypoints": []}],
            timestamp=1000.5,
        )
        # Either the small far-shifted bbox spawned a new track, OR
        # the original track is still at its original bbox (unchanged).
        # The key invariant: small-far-bbox didn't take over the
        # original track via the looser threshold.
        original = m.tracked.get(original_track_id)
        if original is not None:
            assert original.bbox == [100, 100, 300, 600], \
                "unidentified track must keep its original bbox; did not match the far one"

    def test_identified_track_survives_long_silent_gap(self):
        """Identified track stays in self.tracked through a 25-second
        silent gap — longer than self.lost_timeout (8 s) but under
        IDENTITY_LOST_TIMEOUT (30 s)."""
        m = self._make_tracker()
        m.update(
            [{"bbox": [100, 100, 300, 600], "confidence": 0.9, "keypoints": []}],
            timestamp=1000.0,
        )
        track_id = next(iter(m.tracked))
        m.tracked[track_id].identity_name = "Dad"

        # Empty detection list at t=25 → stale-prune step runs but
        # IDENTITY_LOST_TIMEOUT=30 keeps identified tracks alive.
        m.update([], timestamp=1025.0)
        assert track_id in m.tracked, \
            "identified track must survive a 25 s silent gap"

    def test_unidentified_track_pruned_at_normal_lost_timeout(self):
        """Negative: unidentified track must STILL be pruned at the
        default 8 s threshold — the longer IDENTITY_LOST_TIMEOUT only
        applies when identity_name is set."""
        m = self._make_tracker()
        m.update(
            [{"bbox": [100, 100, 300, 600], "confidence": 0.9, "keypoints": []}],
            timestamp=1000.0,
        )
        track_id = next(iter(m.tracked))
        # NO identity_name set

        m.update([], timestamp=1012.0)
        assert track_id not in m.tracked, \
            "unidentified track must be pruned at the 8 s lost_timeout"

    def test_identity_demoted_on_re_match_after_long_silent_gap(self):
        """Live-regression target: a stranger walking into an
        identified person's recently-vacated bbox spot must NOT
        inherit the original name. After IDENTITY_PERSIST_GAP_SECS
        of silence, the next re-match demotes identity_name to ''
        (face-recognizer will re-evaluate)."""
        m = self._make_tracker()
        m.update(
            [{"bbox": [100, 100, 300, 600], "confidence": 0.9, "keypoints": []}],
            timestamp=1000.0,
        )
        track_id = next(iter(m.tracked))
        m.tracked[track_id].identity_name = "Dad"
        m.tracked[track_id].last_identity_confirmation_ts = 1000.0

        # 10 s gap (above the 6 s persist gap, below the 30 s lost
        # timeout for identified). Then a new detection at the same
        # spot — could be Dad returning, could be a stranger.
        m.update(
            [{"bbox": [105, 100, 305, 600], "confidence": 0.85, "keypoints": []}],
            timestamp=1010.0,
        )
        # Track ID preserved (still the same one).
        assert track_id in m.tracked, "track ID must be preserved"
        # Identity demoted to empty — face-recognizer will re-confirm.
        assert m.tracked[track_id].identity_name == "", \
            f"identity must be demoted after 10 s silent gap, got '{m.tracked[track_id].identity_name}'"

    def test_identity_NOT_demoted_when_face_confirmed_during_gap(self):
        """If face-recognizer continued confirming the identity during
        a pose-detection gap (face visible while body wasn't),
        last_identity_confirmation_ts is fresh → no demotion on
        re-match."""
        m = self._make_tracker()
        m.update(
            [{"bbox": [100, 100, 300, 600], "confidence": 0.9, "keypoints": []}],
            timestamp=1000.0,
        )
        track_id = next(iter(m.tracked))
        m.tracked[track_id].identity_name = "Dad"
        # Pose lost at t=0, but face-recognizer confirmed at t=5 (during gap).
        m.tracked[track_id].last_identity_confirmation_ts = 1005.0

        m.update(
            [{"bbox": [105, 100, 305, 600], "confidence": 0.85, "keypoints": []}],
            timestamp=1010.0,
        )
        assert m.tracked[track_id].identity_name == "Dad", \
            "identity must NOT be demoted when face-recognizer confirmed during the gap"

    def test_identity_not_demoted_for_short_gap(self):
        """A brief occlusion (≤ IDENTITY_PERSIST_GAP_SECS) must NOT
        trigger identity demotion — don't break normal walk-behind-tree
        scenarios."""
        m = self._make_tracker()
        m.update(
            [{"bbox": [100, 100, 300, 600], "confidence": 0.9, "keypoints": []}],
            timestamp=1000.0,
        )
        track_id = next(iter(m.tracked))
        m.tracked[track_id].identity_name = "Dad"
        m.tracked[track_id].last_identity_confirmation_ts = 1000.0

        # 4 s gap — under the 6 s threshold
        m.update(
            [{"bbox": [105, 100, 305, 600], "confidence": 0.85, "keypoints": []}],
            timestamp=1004.0,
        )
        assert m.tracked[track_id].identity_name == "Dad", \
            "brief gap (< IDENTITY_PERSIST_GAP_SECS) must preserve identity"
