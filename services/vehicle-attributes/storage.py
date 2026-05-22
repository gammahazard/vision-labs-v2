"""Filesystem layout writer for vehicle-attributes Phase 1/3.

Writes per-track directories at:
    /data/snapshots/vehicles/{camera}/{date}/{track_id}/
        hero.jpg            -- highest-confidence crop
        angle_NN.jpg        -- remaining crops (zero-padded index)
        metadata.json       -- track metadata + attribute block

Phase 1's metadata.json attribute block is all-null placeholders. Phase 3
populates it by passing `attributes=` to flush_buffer_to_disk. Omitting the
kwarg preserves the Phase 1 null-block behavior (backward compat).
"""
import json
import logging
import os
from datetime import datetime
from typing import Optional

from buffer import TrackBuffer

logger = logging.getLogger("vehicle-attributes.storage")


def _date_str_from_first_seen(first_seen: float) -> str:
    """YYYY-MM-DD in local time (container TZ from LOCATION_TIMEZONE)."""
    return datetime.fromtimestamp(first_seen).strftime("%Y-%m-%d")


def flush_buffer_to_disk(
    buf: TrackBuffer,
    last_seen: float,
    event_kind: str,           # "drive_by" | "idle"
    vehicle_class: str,        # "car" | "truck" | "bus" | ...
    snapshot_root: str,
    attributes: Optional[dict] = None,
) -> None:
    """Write the buffer to /data/snapshots/vehicles/{cam}/{date}/{track_id}/.

    `attributes`: optional dict from the Phase 3 classifier. When provided,
    becomes the `attributes` block in metadata.json. When None (Phase 1
    behavior + Phase 3 with ENABLE_CLASSIFIER=0), the block is all-null.
    """
    if not buf.crops:
        logger.debug(f"Flush {buf.track_id}: empty buffer, skipping")
        return

    date_str = _date_str_from_first_seen(buf.first_seen)
    track_dir = os.path.join(snapshot_root, buf.camera_id, date_str,
                             buf.track_id)
    os.makedirs(track_dir, exist_ok=True)

    hero_idx = buf.hero_index()

    hero_path = os.path.join(track_dir, "hero.jpg")
    with open(hero_path, "wb") as fh:
        fh.write(buf.crops[hero_idx])

    angle_n = 1
    for i, crop in enumerate(buf.crops):
        if i == hero_idx:
            continue
        angle_path = os.path.join(track_dir, f"angle_{angle_n:02d}.jpg")
        with open(angle_path, "wb") as fh:
            fh.write(crop)
        angle_n += 1

    if attributes is None:
        attributes = {
            "color": None, "color_confidence": None,
            "body_type": None, "body_type_confidence": None,
            "make": None, "make_confidence": None,
            "model": None, "model_confidence": None,
        }

    meta = {
        "track_id": buf.track_id,
        "camera_id": buf.camera_id,
        "first_seen": buf.first_seen,
        "last_seen": last_seen,
        "duration_seconds": round(last_seen - buf.first_seen, 2),
        "event_kind": event_kind,
        "vehicle_class": vehicle_class,
        "hero_frame_index": hero_idx,
        "voting_samples": len(buf.crops),
        "attributes": attributes,
        "snapshot_bbox": buf.bboxes[hero_idx],
    }
    meta_path = os.path.join(track_dir, "metadata.json")
    with open(meta_path, "w") as fh:
        json.dump(meta, fh, indent=2)

    logger.info(
        f"Flushed {buf.track_id} → {track_dir} "
        f"({len(buf.crops)} crops, hero=angle_{hero_idx}, kind={event_kind})"
    )
