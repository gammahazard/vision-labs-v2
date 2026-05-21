"""Per-track HD-crop buffer for vehicle-attributes Phase 1.

The buffer accumulates HD JPEG crops keyed by track_id as the tracker emits
`vehicle_sample` events. On `vehicle_left` or `vehicle_idle` the buffer is
flushed to disk by `storage.py`.

Phase 1 caps the buffer at 8 crops (spec §2.3). Phase 3 will run a classifier
across the buffered crops with weighted majority voting.
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TrackBuffer:
    track_id: str
    camera_id: str
    first_seen: float
    crops: list[bytes] = field(default_factory=list)
    confidences: list[float] = field(default_factory=list)
    bboxes: list[list[int]] = field(default_factory=list)
    last_sampled_at: float = 0.0
    max_crops: int = 8

    def append(self, crop: bytes, yolo_conf: float, bbox: list[int]) -> None:
        """Add a crop. Silently drops if already at max_crops.

        First-N policy (not LRU): drive-bys typically show the most angle
        diversity in the first few frames as the car enters the frame.
        """
        if self.is_full():
            return
        self.crops.append(crop)
        self.confidences.append(yolo_conf)
        self.bboxes.append(list(bbox))

    def is_full(self) -> bool:
        return len(self.crops) >= self.max_crops

    def hero_index(self) -> Optional[int]:
        if not self.confidences:
            return None
        return max(range(len(self.confidences)),
                   key=lambda i: self.confidences[i])
