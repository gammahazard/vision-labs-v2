"""
contracts/actions.py — Action classification from body keypoints.

PURPOSE:
    Classifies what a person is DOING based on their body keypoints.
    No new model needed — this is pure math on the 17 COCO keypoints
    that YOLOv8-pose already provides.

RELATIONSHIPS:
    - Used by: services/tracker/tracker.py (calls classify_action per detection)
    - Reads: keypoint data from pose-detector detections
    - Writes: action labels added to tracker events

DETECTABLE ACTIONS:
    - standing: normal upright posture
    - crouching: knees significantly bent, torso low
    - arms_raised: one or both arms above shoulders
    - lying_down: torso nearly horizontal
    - running: wide leg stride (when combined with movement speed)

KEYPOINT INDEX REFERENCE (COCO format):
    0: nose, 1: left_eye, 2: right_eye, 3: left_ear, 4: right_ear,
    5: left_shoulder, 6: right_shoulder, 7: left_elbow, 8: right_elbow,
    9: left_wrist, 10: right_wrist, 11: left_hip, 12: right_hip,
    13: left_knee, 14: right_knee, 15: left_ankle, 16: right_ankle
"""

import math

# Minimum keypoint confidence to consider it visible
MIN_KP_CONF = 0.3

# Keypoint indices
NOSE = 0
L_SHOULDER = 5
R_SHOULDER = 6
L_ELBOW = 7
R_ELBOW = 8
L_WRIST = 9
R_WRIST = 10
L_HIP = 11
R_HIP = 12
L_KNEE = 13
R_KNEE = 14
L_ANKLE = 15
R_ANKLE = 16


# --- Tunable thresholds (named so they're easy to find + adjust) ---
# All thresholds that compare pixel distances are now expressed as
# fractions of an estimated "body scale" so they work across frame
# resolutions and at any distance from the camera. The scale is the
# shoulder-to-hip vertical distance when both pairs are visible, with
# a bbox-derived fallback (~40% of bbox height ≈ torso length).
ARMS_RAISED_RATIO = 0.10           # wrist must be this fraction of body
                                   # scale above the shoulder. Was 30 px.
LYING_TORSO_VERT_RATIO = 0.20      # torso vertical extent below this
                                   # fraction of body scale → lying down.
                                   # Was 50 px absolute.
LYING_HORIZ_OVER_VERT = 1.5        # shoulder-to-hip x-span must exceed
                                   # y-span by this ratio.
CROUCH_KNEE_ANGLE_DEG = 100        # tighter than the old 120° so an
                                   # ordinary seated knee (~90°) doesn't
                                   # trip crouch.
SITTING_KNEE_NEAR_HIP_RATIO = 0.6  # knee y within (this × torso_height)
                                   # of hip y → "knees at hip level".
SITTING_ANKLE_BELOW_KNEE_RATIO = 0.25  # ankle must be at least this
                                   # fraction of body scale below knee
                                   # for "feet are forward of body"
                                   # (sitting in a chair). When ankle is
                                   # at/above knee, it's a deep squat.
MIN_TORSO_PIXELS = 8               # absolute floor so a degenerate
                                   # 1-pixel torso doesn't drive ratios.


def _kp_visible(kp: list) -> bool:
    """Check if a keypoint has sufficient confidence."""
    return len(kp) >= 3 and kp[2] >= MIN_KP_CONF


def _midpoint(kp_a: list, kp_b: list) -> tuple[float, float]:
    """Get the midpoint between two keypoints."""
    return ((kp_a[0] + kp_b[0]) / 2, (kp_a[1] + kp_b[1]) / 2)


def _distance(kp_a: list, kp_b: list) -> float:
    """Euclidean distance between two keypoints."""
    return math.sqrt((kp_a[0] - kp_b[0]) ** 2 + (kp_a[1] - kp_b[1]) ** 2)


def _angle(a: list, b: list, c: list) -> float:
    """
    Angle at point B formed by line segments BA and BC.
    Returns angle in degrees (0-180).
    """
    ba = (a[0] - b[0], a[1] - b[1])
    bc = (c[0] - b[0], c[1] - b[1])

    dot = ba[0] * bc[0] + ba[1] * bc[1]
    mag_ba = math.sqrt(ba[0] ** 2 + ba[1] ** 2)
    mag_bc = math.sqrt(bc[0] ** 2 + bc[1] ** 2)

    if mag_ba == 0 or mag_bc == 0:
        return 0

    cos_angle = max(-1, min(1, dot / (mag_ba * mag_bc)))
    return math.degrees(math.acos(cos_angle))


def _body_scale(kps: list, bbox: list[float] | None) -> float:
    """
    Best-effort estimate of body scale in pixels.

    Priority:
      1. shoulder-to-hip vertical distance (torso length) when both
         shoulders and both hips are visible — the most stable scale
         reference across distance + camera angle.
      2. bbox height × 0.40 (torso is roughly 40% of a standing person).
      3. 30 px last-resort floor — every threshold also has its own
         absolute minimum so we never compare against literally zero.
    """
    # Tolerate short keypoint arrays — production always passes 17 COCO
    # points, but the public docstring promises "any length" so callers
    # with incomplete data shouldn't crash.
    if len(kps) > max(L_SHOULDER, R_SHOULDER, L_HIP, R_HIP) and (
            _kp_visible(kps[L_SHOULDER]) and _kp_visible(kps[R_SHOULDER]) and
            _kp_visible(kps[L_HIP]) and _kp_visible(kps[R_HIP])):
        s_mid = _midpoint(kps[L_SHOULDER], kps[R_SHOULDER])
        h_mid = _midpoint(kps[L_HIP], kps[R_HIP])
        ts = abs(h_mid[1] - s_mid[1])
        if ts >= MIN_TORSO_PIXELS:
            return ts
    if bbox and len(bbox) == 4:
        bh = abs(bbox[3] - bbox[1])
        if bh > 0:
            return bh * 0.40
    return 30.0


def classify_action(keypoints: list[list],
                    bbox: list[float] | None = None) -> dict:
    """
    Classify a person's action based on their COCO keypoints.

    Args:
        keypoints: list of [x, y, confidence] triples (any length;
            individual branches check the keypoints they need).
        bbox: optional [x1, y1, x2, y2] for body-scale fallback when
            shoulders/hips aren't fully visible.

    Returns:
        dict with:
        - action: str — primary action label
        - confidence: float — how confident we are (0-1)
        - details: dict — individual checks (for debugging)

    Notes:
        Resolution order: arms_raised → lying_down → sitting → crouching
        → standing (default). Sitting is intentionally checked BEFORE
        crouching now, with an ankle-below-knee discriminator so a
        person sitting in a chair (knees forward at ~90°) doesn't get
        called "crouching" because of their bent knees.

        All pixel thresholds are scaled by an estimated body scale, so
        the classifier works at any frame resolution / distance.
    """
    if not keypoints:
        return {"action": "unknown", "confidence": 0, "details": {}}

    # Pad short arrays with zero-confidence placeholders so each branch's
    # `_kp_visible(kps[INDEX])` lookups never IndexError. Production
    # pose-detector always emits all 17 COCO points, but the public
    # docstring promises "any length" and the classifier shouldn't crash
    # if a future caller passes a partial.
    kps = list(keypoints)
    if len(kps) < 17:
        kps = kps + [[0, 0, 0.0]] * (17 - len(kps))
    details = {}
    scale = _body_scale(kps, bbox)
    details["body_scale_px"] = round(scale, 1)

    # --- Check: Arms Raised ---
    # Either wrist significantly above its shoulder (scaled by body size).
    arms_offset = max(8.0, scale * ARMS_RAISED_RATIO)
    arms_raised = False
    if _kp_visible(kps[L_WRIST]) and _kp_visible(kps[L_SHOULDER]):
        # In image coords, y increases downward, so wrist.y < shoulder.y = raised
        left_raised = kps[L_WRIST][1] < kps[L_SHOULDER][1] - arms_offset
        details["left_arm_raised"] = left_raised
        if left_raised:
            arms_raised = True

    if _kp_visible(kps[R_WRIST]) and _kp_visible(kps[R_SHOULDER]):
        right_raised = kps[R_WRIST][1] < kps[R_SHOULDER][1] - arms_offset
        details["right_arm_raised"] = right_raised
        if right_raised:
            arms_raised = True

    if arms_raised:
        return {"action": "arms_raised", "confidence": 0.8, "details": details}

    # --- Check: Lying Down ---
    # Torso oriented horizontally — shoulder-hip x-span exceeds y-span
    # by 1.5×, AND the y-span itself is small relative to body scale.
    if (_kp_visible(kps[L_SHOULDER]) and _kp_visible(kps[R_SHOULDER]) and
            _kp_visible(kps[L_HIP]) and _kp_visible(kps[R_HIP])):
        shoulder_mid = _midpoint(kps[L_SHOULDER], kps[R_SHOULDER])
        hip_mid = _midpoint(kps[L_HIP], kps[R_HIP])
        torso_height_diff = abs(shoulder_mid[1] - hip_mid[1])
        torso_width_diff = abs(shoulder_mid[0] - hip_mid[0])
        lying_y_max = max(10.0, scale * LYING_TORSO_VERT_RATIO)

        if (torso_width_diff > torso_height_diff * LYING_HORIZ_OVER_VERT
                and torso_height_diff < lying_y_max):
            details["torso_horizontal"] = True
            return {"action": "lying_down", "confidence": 0.7, "details": details}

    # --- Check: Sitting ---  (now BEFORE crouching)
    # Hips roughly at knee level (knees forward, thighs near-horizontal)
    # AND either ankles hidden under furniture OR ankles measurably
    # below the knee — the latter is what distinguishes a chair pose
    # from a squat where ankles are folded up near the knees/hips.
    if (_kp_visible(kps[L_SHOULDER]) and _kp_visible(kps[R_SHOULDER]) and
            _kp_visible(kps[L_HIP]) and _kp_visible(kps[R_HIP])):
        shoulder_mid = _midpoint(kps[L_SHOULDER], kps[R_SHOULDER])
        hip_mid = _midpoint(kps[L_HIP], kps[R_HIP])
        torso_height = hip_mid[1] - shoulder_mid[1]  # positive = upright

        knees_visible = (_kp_visible(kps[L_KNEE]) and
                         _kp_visible(kps[R_KNEE]))
        ankles_visible = (_kp_visible(kps[L_ANKLE]) and
                          _kp_visible(kps[R_ANKLE]))
        ankles_hidden = (not _kp_visible(kps[L_ANKLE]) and
                         not _kp_visible(kps[R_ANKLE]))

        knee_at_hip = False
        feet_forward = False
        if knees_visible and torso_height >= MIN_TORSO_PIXELS:
            knee_mid_y = (kps[L_KNEE][1] + kps[R_KNEE][1]) / 2
            # Knees near hip height (within SITTING_KNEE_NEAR_HIP_RATIO
            # of torso height — comment-vs-code drift fixed: this is
            # 60% by default; raise to tighten if you see false sittings).
            if abs(knee_mid_y - hip_mid[1]) < torso_height * SITTING_KNEE_NEAR_HIP_RATIO:
                knee_at_hip = True
                details["knee_at_hip_level"] = True

            # Discriminate sitting (feet forward, ankles below knees)
            # from crouching (feet folded under, ankles at/above knees).
            if ankles_visible:
                ankle_mid_y = (kps[L_ANKLE][1] + kps[R_ANKLE][1]) / 2
                ankle_drop = ankle_mid_y - knee_mid_y  # positive = ankles below
                feet_forward_min = max(
                    8.0, scale * SITTING_ANKLE_BELOW_KNEE_RATIO
                )
                if ankle_drop > feet_forward_min:
                    feet_forward = True
                    details["feet_forward_of_body"] = True

        # Require POSITIVE evidence of sitting — knees must be visible AND
        # at hip level. Absence of ankle detection is NOT evidence of sitting:
        #   - Elevated cameras crop feet of standing people.
        #   - Indoor scenes occlude legs behind furniture.
        #   - Low light makes YOLO miss low-contrast ankle keypoints.
        # The old "torso-only → sitting" branch produced constant false
        # positives on basement cams and high-mounted exterior cams. Removed.
        is_sitting = False
        if knee_at_hip and ankles_visible and feet_forward:
            # Strong evidence: knees forward at hip level + ankles below knees.
            is_sitting = True
            sit_confidence = 0.75
        elif knee_at_hip and ankles_hidden and torso_height >= MIN_TORSO_PIXELS:
            # Knees are clearly at hip level (thighs near-horizontal) but
            # feet are hidden. Tighten the bar: require knee_mid_y to be
            # within 40% of torso height of hip_mid (vs the 60% standing
            # check) so a normal stand with happened-to-fail-detect ankles
            # doesn't trip this.
            knee_mid_y = (kps[L_KNEE][1] + kps[R_KNEE][1]) / 2
            if abs(knee_mid_y - hip_mid[1]) < torso_height * 0.4:
                is_sitting = True
                sit_confidence = 0.6  # lower — ankles missing = less certain

        if is_sitting:
            details["sitting"] = True
            return {"action": "sitting", "confidence": sit_confidence, "details": details}

    # --- Check: Crouching ---  (now AFTER sitting, tightened threshold)
    # Knee angle deeply bent. Threshold tightened from 120° to 100° so
    # ordinary chair-sitting (knee ≈ 90°) doesn't catch here when the
    # sitting check didn't fire (e.g. ankles missing + knees visible).
    is_crouching = False
    if (_kp_visible(kps[L_HIP]) and _kp_visible(kps[L_KNEE]) and
            _kp_visible(kps[L_ANKLE])):
        left_knee_angle = _angle(kps[L_HIP], kps[L_KNEE], kps[L_ANKLE])
        details["left_knee_angle"] = round(left_knee_angle, 1)
        if left_knee_angle < CROUCH_KNEE_ANGLE_DEG:
            is_crouching = True

    if (_kp_visible(kps[R_HIP]) and _kp_visible(kps[R_KNEE]) and
            _kp_visible(kps[R_ANKLE])):
        right_knee_angle = _angle(kps[R_HIP], kps[R_KNEE], kps[R_ANKLE])
        details["right_knee_angle"] = round(right_knee_angle, 1)
        if right_knee_angle < CROUCH_KNEE_ANGLE_DEG:
            is_crouching = True

    if is_crouching:
        return {"action": "crouching", "confidence": 0.7, "details": details}

    # --- Default: Standing ---
    return {"action": "standing", "confidence": 0.6, "details": details}
