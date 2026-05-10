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


def classify_action(keypoints: list[list]) -> dict:
    """
    Classify a person's action based on their 17 COCO keypoints.

    Args:
        keypoints: List of 17 keypoints, each [x, y, confidence].

    Returns:
        dict with:
        - action: str — primary action label
        - confidence: float — how confident we are (0-1)
        - details: dict — individual checks (for debugging)
    """
    if not keypoints or len(keypoints) < 17:
        return {"action": "unknown", "confidence": 0, "details": {}}

    kps = keypoints
    details = {}

    # --- Check: Arms Raised ---
    # If either wrist is significantly above its shoulder
    arms_raised = False
    if _kp_visible(kps[L_WRIST]) and _kp_visible(kps[L_SHOULDER]):
        # In image coords, y increases downward, so wrist.y < shoulder.y = raised
        left_raised = kps[L_WRIST][1] < kps[L_SHOULDER][1] - 30
        details["left_arm_raised"] = left_raised
        if left_raised:
            arms_raised = True

    if _kp_visible(kps[R_WRIST]) and _kp_visible(kps[R_SHOULDER]):
        right_raised = kps[R_WRIST][1] < kps[R_SHOULDER][1] - 30
        details["right_arm_raised"] = right_raised
        if right_raised:
            arms_raised = True

    if arms_raised:
        return {"action": "arms_raised", "confidence": 0.8, "details": details}

    # --- Check: Lying Down ---
    # If shoulders and hips are at roughly the same height (horizontal torso)
    if (_kp_visible(kps[L_SHOULDER]) and _kp_visible(kps[R_SHOULDER]) and
            _kp_visible(kps[L_HIP]) and _kp_visible(kps[R_HIP])):
        shoulder_mid = _midpoint(kps[L_SHOULDER], kps[R_SHOULDER])
        hip_mid = _midpoint(kps[L_HIP], kps[R_HIP])
        torso_height_diff = abs(shoulder_mid[1] - hip_mid[1])
        torso_width_diff = abs(shoulder_mid[0] - hip_mid[0])

        # If torso is more horizontal than vertical → lying down
        if torso_width_diff > torso_height_diff * 1.5 and torso_height_diff < 50:
            details["torso_horizontal"] = True
            return {"action": "lying_down", "confidence": 0.7, "details": details}

    # --- Check: Crouching ---
    # Knee angle significantly bent (< 120°) AND hips low relative to image height
    is_crouching = False
    if (_kp_visible(kps[L_HIP]) and _kp_visible(kps[L_KNEE]) and
            _kp_visible(kps[L_ANKLE])):
        left_knee_angle = _angle(kps[L_HIP], kps[L_KNEE], kps[L_ANKLE])
        details["left_knee_angle"] = round(left_knee_angle, 1)
        if left_knee_angle < 120:
            is_crouching = True

    if (_kp_visible(kps[R_HIP]) and _kp_visible(kps[R_KNEE]) and
            _kp_visible(kps[R_ANKLE])):
        right_knee_angle = _angle(kps[R_HIP], kps[R_KNEE], kps[R_ANKLE])
        details["right_knee_angle"] = round(right_knee_angle, 1)
        if right_knee_angle < 120:
            is_crouching = True

    if is_crouching:
        return {"action": "crouching", "confidence": 0.7, "details": details}

    # --- Check: Sitting ---
    # Hips roughly at knee level, torso upright (shoulders well above hips)
    if (_kp_visible(kps[L_SHOULDER]) and _kp_visible(kps[R_SHOULDER]) and
            _kp_visible(kps[L_HIP]) and _kp_visible(kps[R_HIP])):
        shoulder_mid = _midpoint(kps[L_SHOULDER], kps[R_SHOULDER])
        hip_mid = _midpoint(kps[L_HIP], kps[R_HIP])
        torso_height = hip_mid[1] - shoulder_mid[1]  # positive = hips below shoulders

        # Check if knees are roughly at hip height (sitting in chair)
        knee_at_hip = False
        if _kp_visible(kps[L_KNEE]) and _kp_visible(kps[R_KNEE]):
            knee_mid_y = (kps[L_KNEE][1] + kps[R_KNEE][1]) / 2
            # Knees near hip height (within 40% of torso height)
            if torso_height > 20 and abs(knee_mid_y - hip_mid[1]) < torso_height * 0.6:
                knee_at_hip = True
                details["knee_at_hip_level"] = True

        # Also detect sitting when ankles aren't visible (under desk)
        # but torso is upright and bbox is short relative to width
        ankles_hidden = (not _kp_visible(kps[L_ANKLE]) and
                         not _kp_visible(kps[R_ANKLE]))

        if knee_at_hip or (ankles_hidden and torso_height > 20):
            details["sitting"] = True
            return {"action": "sitting", "confidence": 0.65, "details": details}

    # --- Default: Standing ---
    return {"action": "standing", "confidence": 0.6, "details": details}
