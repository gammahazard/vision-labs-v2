"""Per-track HD-crop buffer for vehicle-attributes Phase 1+3.

Phase 1 used first-N capping. Phase 3 switches to reservoir sampling
(Algorithm R) so the kept crops uniformly span the track's full lifetime
— necessary for the model head's multi-view voting on drive-by tracks
where the entry/mid/exit angles all carry information.
"""
import random
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
    _n_seen: int = 0

    def append(self, crop: bytes, yolo_conf: float, bbox: list[int]) -> None:
        """Reservoir sampling (Algorithm R): once the buffer fills, each
        new sample has probability max_crops/_n_seen of replacing a random
        existing slot. Result: each input has equal probability of being
        in the final reservoir, regardless of arrival order.
        """
        self._n_seen += 1
        if len(self.crops) < self.max_crops:
            self.crops.append(crop)
            self.confidences.append(yolo_conf)
            self.bboxes.append(list(bbox))
            return
        j = random.randrange(self._n_seen)
        if j < self.max_crops:
            self.crops[j] = crop
            self.confidences[j] = yolo_conf
            self.bboxes[j] = list(bbox)

    def is_full(self) -> bool:
        return len(self.crops) >= self.max_crops

    def hero_index(self) -> Optional[int]:
        if not self.confidences:
            return None
        return max(range(len(self.confidences)),
                   key=lambda i: self.confidences[i])
