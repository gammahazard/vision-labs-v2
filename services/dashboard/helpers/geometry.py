"""
services/dashboard/helpers/geometry.py — bbox + zone math.

PURPOSE:
    Pure functions used by the WebSocket overlay loop to:
    - Match the current detection's bbox to a tracker-tracked person (`bbox_iou`)
    - Decide if a detection falls inside a configured dead zone (`in_dead_zone`)

WHY HERE AND NOT INLINE IN server.py:
    Keeping these as standalone functions makes them trivially testable
    (no Redis, no globals, no FastAPI) and keeps server.py focused on
    wiring rather than math.

RELATIONSHIPS:
    - Imported by: services/dashboard/server.py (the WebSocket loop)
    - Delegates to: contracts/time_rules.point_in_polygon (ray-casting)
"""

from contracts.time_rules import point_in_polygon


def bbox_iou(box_a: list, box_b: list) -> float:
    """Compute IoU between two [x1, y1, x2, y2] bounding boxes."""
    xa = max(box_a[0], box_b[0])
    ya = max(box_a[1], box_b[1])
    xb = min(box_a[2], box_b[2])
    yb = min(box_a[3], box_b[3])

    inter = max(0, xb - xa) * max(0, yb - ya)
    if inter == 0:
        return 0.0

    area_a = (box_a[2] - box_a[0]) * (box_a[3] - box_a[1])
    area_b = (box_b[2] - box_b[0]) * (box_b[3] - box_b[1])
    union = area_a + area_b - inter

    return inter / union if union > 0 else 0.0


def in_dead_zone(bbox: list, frame_w: int, frame_h: int, zone_cache: dict) -> bool:
    """
    Check if a bbox center falls inside any zone marked with
    `alert_level == "dead_zone"`.

    Args:
        bbox: [x1, y1, x2, y2] in pixel coords matching `frame_w` x `frame_h`.
        frame_w, frame_h: frame dimensions (used to normalize to 0-1 for the
            polygon test, since zones are stored as normalized coords).
        zone_cache: zones hash decoded from `zones:{camera_id}` Redis hash.

    Returns:
        True if any dead_zone contains the bbox center.
    """
    if not zone_cache or len(bbox) != 4:
        return False

    cx = ((bbox[0] + bbox[2]) / 2) / frame_w
    cy = ((bbox[1] + bbox[3]) / 2) / frame_h

    for zone in zone_cache.values():
        if zone.get("alert_level") != "dead_zone":
            continue
        pts = zone.get("points", [])
        if len(pts) < 3:
            continue
        if point_in_polygon(cx, cy, pts):
            return True
    return False
