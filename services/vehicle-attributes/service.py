"""Per-camera vehicle-attributes service — Phase 1 (capture + group only).

Subscribes to `events:{cam}`. On vehicle_detected opens a TrackBuffer. On
vehicle_sample pulls `frame_hd:{cam}` from Redis, crops, appends to buffer.
On vehicle_left OR vehicle_idle flushes the buffer to disk and removes it.

No classifier in this phase — Phase 3 will add a buffer→prediction step
before the disk flush.

Startup gate: reads cameras:registry to confirm both detect_vehicles AND
detect_vehicle_attributes are true; exits cleanly if either is false.
"""
import json
import logging
import os
import sys
import time

sys.path.insert(0, "/workspace")
import redis  # noqa: E402
import numpy as np  # noqa: E402
import cv2  # noqa: E402

from contracts.redis_client import make_redis_client  # noqa: E402
from contracts.streams import (  # noqa: E402
    EVENT_STREAM,
    HD_FRAME_KEY,
    REGISTRY_KEY,
)

from buffer import TrackBuffer  # noqa: E402
from storage import flush_buffer_to_disk  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("vehicle-attributes")

CAMERA_ID = os.getenv("CAMERA_ID", "")
SNAPSHOT_ROOT = os.getenv("SNAPSHOT_ROOT", "/data/snapshots/vehicles")
MAX_BUFFER_CROPS = int(os.getenv("MAX_BUFFER_CROPS", "8"))
MIN_CROP_AREA_HD_PX = int(os.getenv("MIN_CROP_AREA_HD_PX", "2500"))  # 50×50
CROP_PADDING_PCT = float(os.getenv("CROP_PADDING_PCT", "0.20"))
JPEG_QUALITY = int(os.getenv("JPEG_QUALITY", "85"))


# ---------------------------------------------------------------------------
# Geometry helpers (pure functions — unit-tested)
# ---------------------------------------------------------------------------

def _scale_bbox_sub_to_hd(bbox: list[int],
                          sub_size: tuple[int, int],
                          hd_size: tuple[int, int]) -> list[int]:
    sx = hd_size[0] / sub_size[0]
    sy = hd_size[1] / sub_size[1]
    return [int(bbox[0] * sx), int(bbox[1] * sy),
            int(bbox[2] * sx), int(bbox[3] * sy)]


def _pad_bbox(bbox: list[int], pct: float,
              frame_size: tuple[int, int]) -> list[int]:
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    dx = int(w * pct)
    dy = int(h * pct)
    return [
        max(0, bbox[0] - dx),
        max(0, bbox[1] - dy),
        min(frame_size[0], bbox[2] + dx),
        min(frame_size[1], bbox[3] + dy),
    ]


def _crop_hd_frame(hd_jpeg_bytes: bytes, bbox: list[int]) -> bytes | None:
    arr = np.frombuffer(hd_jpeg_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return None
    crop = img[bbox[1]:bbox[3], bbox[0]:bbox[2]]
    if crop.size == 0:
        return None
    ok, buf = cv2.imencode(".jpg", crop,
                           [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    if not ok:
        return None
    return buf.tobytes()


# ---------------------------------------------------------------------------
# Event handlers (tested in test_vehicle_attributes_service.py)
# ---------------------------------------------------------------------------

def handle_event(event: dict, buffers: dict,
                 r_bin,
                 hd_size: tuple,
                 snapshot_root: str) -> None:
    """Single-event dispatch. `r_bin` is None in unit tests that don't
    exercise the HD-frame fetch path; production passes the binary client."""
    et = event.get("event_type", "")
    if et == "vehicle_detected":
        _open_buffer(event, buffers)
    elif et == "vehicle_sample":
        _accumulate_crop(event, buffers, r_bin, hd_size)
    elif et in ("vehicle_gone", "vehicle_idle"):
        # `vehicle_gone` fires at ghost-buffer expiry for ALL tracks (drive-by
        # + idle-leave). `vehicle_idle` fires mid-life when a parked car
        # crosses the stationary threshold. We previously listened for
        # `vehicle_left` — that's now idle-leave-only (user-facing event),
        # so drive-by tracks would never flush. See
        # contracts/streams.py:VEHICLE_GONE_EVENT.
        _flush(event, buffers, snapshot_root)


def _open_buffer(event: dict, buffers: dict) -> None:
    track_id = event.get("vehicle_id", "")
    if not track_id or track_id in buffers:
        return
    first_seen = float(event.get("vehicle_first_seen") or
                        event.get("timestamp", "0"))
    buffers[track_id] = TrackBuffer(
        track_id=track_id,
        camera_id=event.get("camera_id", ""),
        first_seen=first_seen,
        max_crops=MAX_BUFFER_CROPS,
    )
    logger.debug(f"opened buffer for {track_id}")


def _accumulate_crop(event: dict, buffers: dict,
                     r_bin, hd_size: tuple) -> None:
    track_id = event.get("vehicle_id", "")
    buf = buffers.get(track_id)
    if buf is None:
        # Sample arrived before detected — open lazily.
        _open_buffer(event, buffers)
        buf = buffers[track_id]
    if buf.is_full() or r_bin is None:
        return

    cam = event.get("camera_id", "")
    # Prefer the per-sample HD snapshot key paired by the tracker at the
    # exact moment the bbox was computed. Fixes the drift bug where the
    # generic `frame_hd:{cam}` key may contain a frame from a different
    # moment than the bbox — for fast-moving cars that drift = car has
    # moved out of the bbox region, crop catches empty road. Falls back
    # to the legacy generic key if the per-sample key is missing or has
    # already expired (60 s TTL on those).
    hd_snapshot_key = event.get("hd_snapshot_key", "")
    hd_bytes = None
    if hd_snapshot_key:
        hd_bytes = r_bin.get(hd_snapshot_key)
    if hd_bytes is None:
        hd_bytes = r_bin.get(HD_FRAME_KEY.format(camera_id=cam))
    if hd_bytes is None:
        logger.debug(f"HD frame missing for {cam} — skip sample {track_id}")
        return

    bbox_sub = json.loads(event.get("bbox", "[]"))
    if len(bbox_sub) != 4:
        return
    # Order: scale → pad → crop (spec §2.3)
    bbox_hd = _scale_bbox_sub_to_hd(bbox_sub,
                                    sub_size=(896, 512),
                                    hd_size=hd_size)
    bbox_padded = _pad_bbox(bbox_hd, CROP_PADDING_PCT, frame_size=hd_size)
    area = (bbox_padded[2] - bbox_padded[0]) * (bbox_padded[3] - bbox_padded[1])
    if area < MIN_CROP_AREA_HD_PX:
        return

    crop = _crop_hd_frame(hd_bytes, bbox_padded)
    if crop is None:
        return
    yolo_conf = float(event.get("vehicle_confidence", "0") or 0)
    buf.append(crop=crop, yolo_conf=yolo_conf, bbox=bbox_padded)
    buf.last_sampled_at = time.monotonic()


def _flush(event: dict, buffers: dict,
           snapshot_root: str) -> None:
    track_id = event.get("vehicle_id", "")
    buf = buffers.pop(track_id, None)
    if buf is None:
        return
    last_seen = float(event.get("timestamp", "0") or 0)
    # Classify the track: `vehicle_idle` is explicitly idle. `vehicle_gone`
    # carries `was_idle` (str "True"/"False") so consumers don't have to
    # re-derive from duration. Defaults to drive_by when neither signal says
    # idle (covers any future event-shape changes safely).
    if event.get("event_type") == "vehicle_idle":
        event_kind = "idle"
    elif event.get("was_idle") == "True":
        event_kind = "idle"
    else:
        event_kind = "drive_by"
    flush_buffer_to_disk(
        buf,
        last_seen=last_seen,
        event_kind=event_kind,
        vehicle_class=event.get("vehicle_class", ""),
        snapshot_root=snapshot_root,
    )


# ---------------------------------------------------------------------------
# Startup gate + main loop (not unit-tested; covered by E2E in Task 15)
# ---------------------------------------------------------------------------

def _check_registry_wants_attributes(r) -> bool:
    """Read cameras:registry. Exit cleanly if either flag is false."""
    raw = r.hget(REGISTRY_KEY, CAMERA_ID)
    if not raw:
        logger.warning(f"camera {CAMERA_ID} not in registry — exiting")
        return False
    cam = json.loads(raw)
    if cam.get("detect_vehicles") is False:
        logger.info(f"{CAMERA_ID}: detect_vehicles=false — exiting cleanly")
        return False
    if cam.get("detect_vehicle_attributes") is not True:
        logger.info(
            f"{CAMERA_ID}: detect_vehicle_attributes=false — exiting cleanly"
        )
        return False
    return True


def main() -> int:
    if not CAMERA_ID:
        logger.error("CAMERA_ID env var not set — exiting")
        return 1

    r = make_redis_client()
    r_bin = make_redis_client(decode_responses=False)

    if not _check_registry_wants_attributes(r):
        return 0

    stream_key = EVENT_STREAM.format(camera_id=CAMERA_ID)
    logger.info(
        f"vehicle-attributes-{CAMERA_ID} started — subscribing to {stream_key}"
    )

    buffers = {}
    last_id = "$"
    hd_size = (
        int(os.getenv("HD_FRAME_WIDTH", "2304")),
        int(os.getenv("HD_FRAME_HEIGHT", "1296")),
    )

    while True:
        try:
            result = r.xread({stream_key: last_id}, block=2000, count=50)
        except redis.RedisError as e:
            logger.warning(f"xread error: {e}; retrying in 1s")
            time.sleep(1.0)
            continue

        if not result:
            continue

        for _stream, entries in result:
            for entry_id, fields in entries:
                last_id = entry_id
                try:
                    handle_event(fields, buffers, r_bin, hd_size,
                                 SNAPSHOT_ROOT)
                except Exception as e:
                    logger.exception(
                        f"handle_event failed for "
                        f"{fields.get('event_type')}: {e}"
                    )


if __name__ == "__main__":
    sys.exit(main())
