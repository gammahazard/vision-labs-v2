"""
tests/test_tracker.py — Real tests for the tracker's core algorithms.

Tests IoU computation, TrackedPerson state management, and direction estimation
using actual bounding box coordinates. These are the algorithms that determine
whether two detections across frames are the same person.

NO mocks on the core logic — only Redis is faked (for PersonTracker tests).
"""

import os
import sys
import time
import pytest

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
    def test_direction_unknown_few_frames(self):
        """Direction is 'unknown' when fewer than 3 positions."""
        p = TrackedPerson("p1", [100, 100, 200, 300], 1000.0)
        assert p.direction == "unknown"
        p.update([105, 100, 205, 300], 1001.0)
        assert p.direction == "unknown"

    def test_direction_stationary(self):
        """Direction is 'stationary' when center moves < 20px."""
        p = TrackedPerson("p1", [100, 100, 200, 300], 1000.0)
        p.update([102, 100, 202, 300], 1001.0)
        p.update([104, 100, 204, 300], 1002.0)
        assert p.direction == "stationary"

    def test_direction_right(self):
        """Direction is 'right' when center moves right > 20px."""
        p = TrackedPerson("p1", [100, 100, 200, 300], 1000.0)
        p.update([130, 100, 230, 300], 1001.0)
        p.update([160, 100, 260, 300], 1002.0)
        assert p.direction == "right"

    def test_direction_left(self):
        """Direction is 'left' when center moves left > 20px."""
        p = TrackedPerson("p1", [200, 100, 300, 300], 1000.0)
        p.update([170, 100, 270, 300], 1001.0)
        p.update([140, 100, 240, 300], 1002.0)
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
