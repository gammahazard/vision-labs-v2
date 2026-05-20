"""tracker/core/iou.py — bounding-box geometry helpers."""

def compute_iou(box_a: list, box_b: list) -> float:
    """
    Compute Intersection over Union between two bounding boxes.

    Each box is [x1, y1, x2, y2].
    Returns a float between 0 (no overlap) and 1 (perfect overlap).

    This is the core of our tracking — if two boxes in consecutive frames
    overlap significantly, we assume they're the same person.
    """
    # Intersection coordinates
    x1 = max(box_a[0], box_b[0])
    y1 = max(box_a[1], box_b[1])
    x2 = min(box_a[2], box_b[2])
    y2 = min(box_a[3], box_b[3])

    # Intersection area (0 if no overlap)
    intersection = max(0, x2 - x1) * max(0, y2 - y1)

    # Union area
    area_a = (box_a[2] - box_a[0]) * (box_a[3] - box_a[1])
    area_b = (box_b[2] - box_b[0]) * (box_b[3] - box_b[1])
    union = area_a + area_b - intersection

    if union == 0:
        return 0.0

    return intersection / union
