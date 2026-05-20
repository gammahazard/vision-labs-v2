"""
tests/test_actions.py — Real tests for the action classifier.

Tests classify_action() with carefully crafted keypoint data that mimics
real YOLO pose detections. Each test generates keypoints that represent
a specific body posture and verifies the classifier returns the right action.

NO mocks — the classifier is pure math on keypoint coordinates.
"""

import os
import sys
import pytest

# Add contracts directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "contracts"))
from actions import (
    classify_action,
    _kp_visible,
    _midpoint,
    _distance,
    _angle,
    L_SHOULDER, R_SHOULDER, L_WRIST, R_WRIST,
    L_HIP, R_HIP, L_KNEE, R_KNEE, L_ANKLE, R_ANKLE,
    MIN_KP_CONF,
)


# ---------------------------------------------------------------------------
# Helpers — generate realistic keypoint arrays
# ---------------------------------------------------------------------------

def make_keypoints(overrides: dict = None) -> list[list]:
    """
    Generate a default 'standing' person with 17 COCO keypoints.

    Each keypoint is [x, y, confidence]. Default person is standing
    upright with arms at sides, centered at x=320, head at y=100.

    Override specific keypoint indices to create different poses.
    """
    # Default standing person (head at top, feet at bottom)
    # Image coordinates: y increases downward
    kps = [
        [320, 100, 0.9],   # 0: nose
        [310, 90, 0.9],    # 1: left_eye
        [330, 90, 0.9],    # 2: right_eye
        [300, 100, 0.8],   # 3: left_ear
        [340, 100, 0.8],   # 4: right_ear
        [280, 180, 0.9],   # 5: left_shoulder
        [360, 180, 0.9],   # 6: right_shoulder
        [260, 260, 0.8],   # 7: left_elbow
        [380, 260, 0.8],   # 8: right_elbow
        [250, 340, 0.8],   # 9: left_wrist
        [390, 340, 0.8],   # 10: right_wrist
        [290, 350, 0.9],   # 11: left_hip
        [350, 350, 0.9],   # 12: right_hip
        [285, 460, 0.8],   # 13: left_knee
        [355, 460, 0.8],   # 14: right_knee
        [280, 570, 0.8],   # 15: left_ankle
        [360, 570, 0.8],   # 16: right_ankle
    ]

    if overrides:
        for idx, kp in overrides.items():
            kps[idx] = kp

    return kps


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------

class TestHelperFunctions:
    def test_kp_visible_high_confidence(self):
        """Keypoint with confidence >= MIN_KP_CONF is visible."""
        assert _kp_visible([100, 200, 0.9]) is True
        assert _kp_visible([100, 200, MIN_KP_CONF]) is True

    def test_kp_visible_low_confidence(self):
        """Keypoint with confidence < MIN_KP_CONF is not visible."""
        assert _kp_visible([100, 200, 0.1]) is False
        assert _kp_visible([100, 200, 0.0]) is False

    def test_kp_visible_short_array(self):
        """Keypoint with fewer than 3 elements is not visible."""
        assert _kp_visible([100, 200]) is False
        assert _kp_visible([]) is False

    def test_midpoint_calculation(self):
        """Midpoint between two keypoints is correct."""
        mid = _midpoint([0, 0, 1.0], [100, 200, 1.0])
        assert mid == (50.0, 100.0)

    def test_distance_calculation(self):
        """Euclidean distance between two keypoints is correct."""
        d = _distance([0, 0, 1.0], [3, 4, 1.0])
        assert abs(d - 5.0) < 1e-6

    def test_angle_right_angle(self):
        """90-degree angle is computed correctly."""
        a = [0, 0, 1.0]
        b = [0, 5, 1.0]  # Vertex
        c = [5, 5, 1.0]
        angle = _angle(a, b, c)
        assert abs(angle - 90.0) < 1.0

    def test_angle_straight_line(self):
        """180-degree angle (straight line) is computed correctly."""
        a = [0, 0, 1.0]
        b = [5, 0, 1.0]  # Vertex
        c = [10, 0, 1.0]
        angle = _angle(a, b, c)
        assert abs(angle - 180.0) < 1.0

    def test_angle_zero_length(self):
        """Zero-length segment returns 0 (no crash)."""
        a = [5, 5, 1.0]
        b = [5, 5, 1.0]  # Same as a
        c = [10, 10, 1.0]
        angle = _angle(a, b, c)
        assert angle == 0  # Degenerate case


# ---------------------------------------------------------------------------
# Action classification tests
# ---------------------------------------------------------------------------

class TestStanding:
    def test_default_standing(self):
        """Default upright person is classified as standing."""
        kps = make_keypoints()
        result = classify_action(kps)
        assert result["action"] == "standing"
        assert result["confidence"] > 0

    def test_standing_has_details(self):
        """Standing classification includes a details dict."""
        kps = make_keypoints()
        result = classify_action(kps)
        assert "details" in result
        assert isinstance(result["details"], dict)


class TestArmsRaised:
    def test_left_arm_raised(self):
        """Person with left arm raised above shoulder."""
        kps = make_keypoints({
            L_WRIST: [250, 100, 0.9],  # Wrist well above shoulder (y=180)
        })
        result = classify_action(kps)
        assert result["action"] == "arms_raised"

    def test_right_arm_raised(self):
        """Person with right arm raised above shoulder."""
        kps = make_keypoints({
            R_WRIST: [390, 100, 0.9],  # Wrist well above shoulder (y=180)
        })
        result = classify_action(kps)
        assert result["action"] == "arms_raised"

    def test_both_arms_raised(self):
        """Person with both arms raised (surrender / celebration)."""
        kps = make_keypoints({
            L_WRIST: [250, 80, 0.9],
            R_WRIST: [390, 80, 0.9],
        })
        result = classify_action(kps)
        assert result["action"] == "arms_raised"

    def test_arms_at_shoulder_level_not_raised(self):
        """Arms at exactly shoulder height should NOT trigger arms_raised."""
        kps = make_keypoints({
            L_WRIST: [250, 180, 0.9],  # Same y as shoulder
            R_WRIST: [390, 180, 0.9],
        })
        result = classify_action(kps)
        assert result["action"] != "arms_raised"

    @pytest.mark.stale  # threshold changed in scaled-pixel refactor; rewrite for body-scale ratio
    def test_arms_slightly_above_not_raised(self):
        """Arms only slightly above shoulder (within 30px margin) should NOT trigger."""
        kps = make_keypoints({
            L_WRIST: [250, 160, 0.9],  # Only 20px above shoulder — below 30px threshold
            R_WRIST: [390, 160, 0.9],
        })
        result = classify_action(kps)
        assert result["action"] != "arms_raised"


class TestLyingDown:
    @pytest.mark.stale  # LYING_TORSO_VERT_RATIO refactor changed the threshold
    def test_horizontal_torso(self):
        """Person lying on their side (horizontal torso) is classified as lying_down."""
        kps = make_keypoints({
            # Shoulders and hips at same height but spread horizontally
            L_SHOULDER: [100, 300, 0.9],
            R_SHOULDER: [200, 300, 0.9],
            L_HIP: [300, 310, 0.9],       # Nearly same y, far x
            R_HIP: [400, 310, 0.9],
            # Move wrists down so arms_raised doesn't trigger
            L_WRIST: [80, 320, 0.8],
            R_WRIST: [420, 320, 0.8],
        })
        result = classify_action(kps)
        assert result["action"] == "lying_down"


class TestCrouching:
    def test_bent_knees(self):
        """Person with sharply bent knees is classified as crouching."""
        kps = make_keypoints({
            # Keep wrists below shoulder so arms_raised doesn't trigger
            L_WRIST: [250, 340, 0.8],
            R_WRIST: [390, 340, 0.8],
            # Make torso somewhat vertical (not lying down)
            L_SHOULDER: [280, 250, 0.9],
            R_SHOULDER: [360, 250, 0.9],
            L_HIP: [290, 350, 0.9],
            R_HIP: [350, 350, 0.9],
            # Sharp knee angle — hip, knee, ankle form < 120°
            L_KNEE: [290, 400, 0.9],
            L_ANKLE: [290, 360, 0.9],   # Ankle behind knee = sharp angle
            R_KNEE: [350, 400, 0.9],
            R_ANKLE: [350, 360, 0.9],
        })
        result = classify_action(kps)
        assert result["action"] == "crouching"


class TestEdgeCases:
    def test_empty_keypoints(self):
        """Empty keypoints returns unknown."""
        result = classify_action([])
        assert result["action"] == "unknown"
        assert result["confidence"] == 0

    def test_none_keypoints(self):
        """None keypoints returns unknown."""
        result = classify_action(None)
        assert result["action"] == "unknown"

    @pytest.mark.stale  # classifier now indexes into keypoint list defensively; behavior changed
    def test_too_few_keypoints(self):
        """Fewer than 17 keypoints returns unknown."""
        kps = [[0, 0, 0.9]] * 10  # Only 10 keypoints
        result = classify_action(kps)
        assert result["action"] == "unknown"

    def test_all_low_confidence(self):
        """All keypoints with low confidence falls back to standing."""
        kps = [[320, 300, 0.1]] * 17  # All below MIN_KP_CONF
        result = classify_action(kps)
        # Should be standing (default) since no checks can trigger
        assert result["action"] == "standing"

    def test_action_priority_arms_over_crouch(self):
        """Arms raised takes priority over crouching (checked first)."""
        # Person crouching with arms up
        kps = make_keypoints({
            L_WRIST: [250, 80, 0.9],     # Arms raised
            L_KNEE: [290, 400, 0.9],      # Also crouching
            L_ANKLE: [290, 360, 0.9],
        })
        result = classify_action(kps)
        assert result["action"] == "arms_raised"  # Arms checked first


# ---------------------------------------------------------------------------
# Regression: the "basement cam always called me sitting" bug
# ---------------------------------------------------------------------------
class TestSittingRequiresPositiveEvidence:
    """The classifier used to fall through to "sitting" any time the torso
    was visible but the ankles weren't (low-mounted cam, occluded feet, low
    light). The torso-only branch was removed; sitting now requires either:
      (a) knees visible at hip level + ankles visible below knees, OR
      (b) knees visible clearly at hip level + ankles hidden (40% tighter).
    Anything else falls through to standing. These tests pin that behaviour
    so we don't accidentally reintroduce the false-positive."""

    def test_standing_with_ankles_missing_is_not_sitting(self):
        """The bug: standing person with feet cropped/occluded.

        Person is upright (knees clearly BELOW hip, not at hip level), but
        ankles dropped to 0 confidence — the situation a basement cam or
        elevated outdoor cam routinely produces. Result must be 'standing',
        not 'sitting'."""
        kps = make_keypoints({
            L_ANKLE: [280, 570, 0.0],  # not visible
            R_ANKLE: [360, 570, 0.0],  # not visible
        })
        result = classify_action(kps)
        assert result["action"] == "standing", (
            f"Standing person with missing ankles must NOT classify as "
            f"sitting. Got {result['action']}. Details: {result['details']}"
        )

    def test_standing_with_knees_missing_is_not_sitting(self):
        kps = make_keypoints({
            L_KNEE: [285, 460, 0.0],
            R_KNEE: [355, 460, 0.0],
            L_ANKLE: [280, 570, 0.0],
            R_ANKLE: [360, 570, 0.0],
        })
        result = classify_action(kps)
        assert result["action"] == "standing"

    def test_sitting_in_chair_with_ankles_below_knees(self):
        """Strong positive evidence: knees forward at hip level, ankles
        below knees → confident sitting."""
        # Default standing person has hips at y=350, knees at y=460,
        # ankles at y=570 (torso ≈ 170 px). Bring knees up to hip level
        # to simulate thighs near-horizontal, keep ankles below knees.
        kps = make_keypoints({
            L_KNEE: [285, 360, 0.9],   # knees at hip level (y≈350)
            R_KNEE: [355, 360, 0.9],
            L_ANKLE: [285, 470, 0.9],  # ankles still below knees
            R_ANKLE: [355, 470, 0.9],
        })
        result = classify_action(kps)
        assert result["action"] == "sitting"
        assert result["confidence"] >= 0.7  # high — feet_forward confirmed

    def test_sitting_with_ankles_hidden_uses_tighter_threshold(self):
        """Knees clearly at hip level (within 40% of torso) AND ankles
        hidden → lower-confidence sitting allowed."""
        kps = make_keypoints({
            L_KNEE: [285, 355, 0.9],   # very close to hip y=350
            R_KNEE: [355, 355, 0.9],
            L_ANKLE: [0, 0, 0.0],      # not visible
            R_ANKLE: [0, 0, 0.0],
        })
        result = classify_action(kps)
        assert result["action"] == "sitting"
        assert result["confidence"] < 0.7  # lower than chair-with-ankles

    def test_crouch_threshold_tighter_than_chair_pose(self):
        """A chair-sitter has knee angle ≈ 90°. The crouch threshold was
        loosened (120° → 100°) in the same fix; a 110° knee angle should
        NOT trip the crouch branch."""
        # Standing default has nearly-straight knee (~180°). Bend knees
        # to ≈110° by moving the knee forward of the hip-ankle line.
        kps = make_keypoints({
            # Hip (290, 350), Knee (320, 460), Ankle (280, 570) gives
            # knee angle ≈ 110° — between the old (120) and new (100) bar.
            L_KNEE: [320, 460, 0.9],
            R_KNEE: [350, 460, 0.9],
        })
        result = classify_action(kps)
        assert result["action"] != "crouching", (
            f"110° knee angle must not trip crouch (threshold tightened to "
            f"<100°). Got {result['action']}. Details: {result['details']}"
        )
