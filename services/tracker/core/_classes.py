"""tracker/core/_classes.py — YOLO vehicle-class equivalence helper.

Extracted from manager.py during the 2026-05-22 mixin split.

YOLOv8 frequently flips a single vehicle between "car", "truck", and
"bus" across consecutive frames (especially trucks/SUVs at partial
occlusion, vans seen end-on, large sedans at angle). The fallback
match paths used to require strict class equality, which split one
physical drive-by into two tracks (observed live: a red pickup at
11:35 became vehicle_0009 "car" + vehicle_0010 "truck" 2 s later,
neither got full per-track samples).

4-wheel motor vehicles (car, truck, bus) are treated as interchangeable
in the fallback paths. Bicycle + motorcycle stay strict — they're
visually distinct enough that YOLO almost never confuses them with
4-wheel vehicles, and false-merging a passing motorcycle into a car
track would be a real data error.
"""

_VEHICLE_CLASS_EQUIV = {"car", "truck", "bus"}


def _class_compatible(a: str, b: str) -> bool:
    """True if two YOLO class labels can plausibly be the same physical
    vehicle observed across frames (YOLO class flicker tolerance)."""
    if a == b:
        return True
    return a in _VEHICLE_CLASS_EQUIV and b in _VEHICLE_CLASS_EQUIV
