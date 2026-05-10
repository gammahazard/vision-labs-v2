"""
services/vehicle-detector/detector.py — Runs YOLOv8 object detection for vehicles.

PURPOSE:
    Detects vehicles (car, truck, bus, motorcycle) in camera frames using YOLOv8s.
    Publishes structured detection results to Redis for the tracker to consume.

    This is the vehicle counterpart to pose-detector. It reads the same frame
    stream but outputs to a separate vehicle detection stream.

RELATIONSHIPS:
    - Reads from: Redis Stream "frames:{camera_id}" (published by camera-ingester)
    - Writes to: Redis Stream "detections:vehicle:{camera_id}" (consumed by tracker)
    - Stream keys defined in: contracts/streams.py
    - Model: YOLOv8s (~500 MB VRAM on RTX 3090)

DATA FLOW:
    camera-ingester → [frames:front_door] → THIS SERVICE → [detections:vehicle:front_door] → tracker

CONFIG (environment variables):
    CAMERA_ID          — Which camera's frames to process (default: "front_door")
    REDIS_HOST         — Redis server hostname (default: "127.0.0.1")
    REDIS_PORT         — Redis server port (default: 6379)
    MODEL_NAME         — YOLO model to use (default: "yolov8s.pt")
    CONFIDENCE_THRESH  — Minimum detection confidence (default: 0.4)
    FRAME_SKIP         — Process every Nth frame (default: 3, saves GPU for fast-moving vehicles)
    CONSUMER_GROUP     — Redis consumer group name for load balancing
    CONSUMER_NAME      — This consumer's unique name within the group
"""

import json
import os
import sys
import time
import signal
import logging

import cv2
import numpy as np
import redis
from ultralytics import YOLO

# Import stream key definitions from contracts (single source of truth)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "contracts"))
from streams import (
    FRAME_STREAM as _FRAME_TMPL,
    DETECTION_STREAM as _DET_TMPL,
    CONFIG_KEY as _CFG_TMPL,
    DETECTION_FRAME_KEY as _DET_FRAME_TMPL,
    GPU_PAUSE_KEY,
    stream_key,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CAMERA_ID = os.getenv("CAMERA_ID", "front_door")
REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
MODEL_NAME = os.getenv("MODEL_NAME", "yolov8s.pt")
CONFIDENCE_THRESH = float(os.getenv("CONFIDENCE_THRESH", "0.35"))
FRAME_SKIP = int(os.getenv("FRAME_SKIP", "3"))
CONSUMER_GROUP = os.getenv("CONSUMER_GROUP", "vehicle_detectors")
CONSUMER_NAME = os.getenv("CONSUMER_NAME", "vdetector_1")

# Stream keys — resolved from contracts/streams.py
FRAME_STREAM = stream_key(_FRAME_TMPL, camera_id=CAMERA_ID)
DETECTION_STREAM = stream_key(_DET_TMPL, detector_type="vehicle", camera_id=CAMERA_ID)
CONFIG_KEY = stream_key(_CFG_TMPL, camera_id=CAMERA_ID)
DETECTION_FRAME = stream_key(_DET_FRAME_TMPL, detector_type="vehicle", camera_id=CAMERA_ID)

# Max detections to keep in the output stream
MAX_DETECTION_STREAM_LEN = 1000

# How often to check Redis for config changes (every N processed frames)
CONFIG_RELOAD_INTERVAL = 25

# COCO class IDs for vehicles
# 2=car, 3=motorcycle, 5=bus, 7=truck
VEHICLE_CLASSES = [2, 3, 5, 7]
VEHICLE_CLASS_NAMES = {2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}

# Minimum bounding box area (pixels²) to accept a vehicle detection.
# Filters out tiny phantom detections from lights, reflections, distant noise.
# A real vehicle at ~50px wide × 50px tall = 2500px²; this threshold filters
# anything smaller while still catching vehicles at moderate distance.
MIN_VEHICLE_BBOX_AREA = int(os.getenv("MIN_VEHICLE_BBOX_AREA", "2500"))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("vehicle-detector")

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    logger.info("Shutdown signal received...")
    _shutdown = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


# ---------------------------------------------------------------------------
# YOLO Model Loading
# ---------------------------------------------------------------------------
def load_model(model_name: str) -> YOLO:
    """
    Load the YOLOv8 object detection model.

    On first run, ultralytics auto-downloads the model weights (~22 MB for yolov8s).
    The model runs on GPU automatically if CUDA is available.
    """
    logger.info(f"Loading YOLO model: {model_name}")
    model = YOLO(model_name)

    import torch
    device = "GPU (CUDA)" if torch.cuda.is_available() else "CPU"
    logger.info(f"Model loaded — CUDA available: {torch.cuda.is_available()}, using: {device}")
    return model


# ---------------------------------------------------------------------------
# Redis Consumer Group Setup
# ---------------------------------------------------------------------------
def setup_consumer_group(r: redis.Redis) -> None:
    """
    Create a Redis consumer group for the frame stream.

    Consumer groups let multiple detector instances share the workload —
    each frame is processed by exactly ONE detector in the group.
    """
    try:
        r.xgroup_create(FRAME_STREAM, CONSUMER_GROUP, id="$", mkstream=True)
        logger.info(f"Created consumer group '{CONSUMER_GROUP}' on '{FRAME_STREAM}'")
    except redis.ResponseError as e:
        if "BUSYGROUP" in str(e):
            logger.info(f"Consumer group '{CONSUMER_GROUP}' already exists")
        else:
            raise


# ---------------------------------------------------------------------------
# Main Detection Loop
# ---------------------------------------------------------------------------
def run():
    logger.info("=" * 60)
    logger.info("VEHICLE DETECTOR — Starting up")
    logger.info(f"  Camera ID:          {CAMERA_ID}")
    logger.info(f"  Redis:              {REDIS_HOST}:{REDIS_PORT}")
    logger.info(f"  Model:              {MODEL_NAME}")
    logger.info(f"  Confidence thresh:  {CONFIDENCE_THRESH}")
    logger.info(f"  Frame skip:         {FRAME_SKIP} (process every {FRAME_SKIP} frame)")
    logger.info(f"  Vehicle classes:    {VEHICLE_CLASSES}")
    logger.info(f"  Input stream:       {FRAME_STREAM}")
    logger.info(f"  Output stream:      {DETECTION_STREAM}")
    logger.info("=" * 60)

    # --- Connect to Redis ---
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=False)
    while not _shutdown:
        try:
            r.ping()
            logger.info("Connected to Redis")
            break
        except redis.ConnectionError:
            logger.warning("Waiting for Redis...")
            time.sleep(2)

    if _shutdown:
        return

    # --- Set up consumer group ---
    setup_consumer_group(r)

    # --- Load YOLO model ---
    model = load_model(MODEL_NAME)

    # --- Detection loop ---
    frames_processed = 0
    frames_skipped = 0
    total_detections = 0
    current_confidence = CONFIDENCE_THRESH

    logger.info("Entering detection loop...")

    while not _shutdown:
        try:
            # --- GPU pause: skip inference while image/video generation is active ---
            try:
                if r.exists(GPU_PAUSE_KEY):
                    if not getattr(run, '_paused_logged', False):
                        logger.info("GPU generation active — pausing inference...")
                        run._paused_logged = True
                    time.sleep(2)
                    continue
                elif getattr(run, '_paused_logged', False):
                    logger.info("GPU generation finished — resuming inference")
                    run._paused_logged = False
            except redis.ConnectionError:
                pass

            # Read next frame from stream via consumer group
            messages = r.xreadgroup(
                CONSUMER_GROUP,
                CONSUMER_NAME,
                {FRAME_STREAM: ">"},
                count=1,
                block=1000,
            )

            if not messages:
                continue

            for stream_name, entries in messages:
                for msg_id, data in entries:
                    # --- Frame skip logic ---
                    frames_skipped += 1
                    if frames_skipped < FRAME_SKIP:
                        # Acknowledge but don't process
                        r.xack(FRAME_STREAM, CONSUMER_GROUP, msg_id)
                        continue
                    frames_skipped = 0

                    # --- Decode frame ---
                    frame_bytes = data.get(b"frame") or data.get(b"frame_bytes")
                    if not frame_bytes:
                        r.xack(FRAME_STREAM, CONSUMER_GROUP, msg_id)
                        continue

                    np_arr = np.frombuffer(frame_bytes, dtype=np.uint8)
                    frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
                    if frame is None:
                        r.xack(FRAME_STREAM, CONSUMER_GROUP, msg_id)
                        continue

                    ts = float(data.get(b"timestamp", time.time()))
                    frame_num = int(data.get(b"frame_number", 0))
                    cam_id = data.get(b"camera_id", CAMERA_ID.encode()).decode()

                    # --- Hot-reload vehicle confidence from Redis config ---
                    frames_processed += 1
                    if frames_processed % CONFIG_RELOAD_INTERVAL == 0:
                        try:
                            cfg = r.hget(CONFIG_KEY, "vehicle_confidence_thresh")
                            if cfg:
                                new_conf = float(cfg)
                                if new_conf != current_confidence:
                                    logger.info(f"Config updated: vehicle confidence {current_confidence} → {new_conf}")
                                    current_confidence = new_conf
                        except (ValueError, redis.ConnectionError):
                            pass

                    # --- Run YOLO inference (vehicle classes only) ---
                    t0 = time.time()
                    results = model.predict(
                        frame,
                        conf=current_confidence,
                        classes=VEHICLE_CLASSES,
                        verbose=False,
                    )
                    inference_ms = (time.time() - t0) * 1000

                    # --- Build detection list ---
                    detections = []
                    if results and results[0].boxes is not None:
                        boxes = results[0].boxes
                        for i in range(len(boxes)):
                            bbox = boxes.xyxy[i].cpu().numpy().tolist()
                            conf = float(boxes.conf[i].cpu().numpy())
                            cls_id = int(boxes.cls[i].cpu().numpy())
                            class_name = VEHICLE_CLASS_NAMES.get(cls_id, f"vehicle_{cls_id}")

                            # Skip tiny detections — filters phantom
                            # hits from lights, reflections, and
                            # distant noise that YOLO misclassifies.
                            bbox_area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
                            if bbox_area < MIN_VEHICLE_BBOX_AREA:
                                continue

                            detections.append({
                                "bbox": [round(c, 1) for c in bbox],
                                "confidence": round(conf, 3),
                                "class_name": class_name,
                                "class_id": cls_id,
                            })

                    # --- Publish to detection stream ---
                    det_msg = {
                        "camera_id": cam_id,
                        "detector_type": "vehicle",
                        "timestamp": str(ts),
                        "frame_number": str(frame_num),
                        "detections": json.dumps(detections),
                        "inference_ms": str(round(inference_ms, 1)),
                    }

                    # Also store the frame bytes for snapshot capture
                    if detections:
                        det_msg["frame_bytes"] = frame_bytes

                    r.xadd(
                        DETECTION_STREAM,
                        det_msg,
                        maxlen=MAX_DETECTION_STREAM_LEN,
                        approximate=True,
                    )

                    # Cache the source frame so the dashboard can draw bboxes on
                    # the exact frame they were computed from (prevents drift)
                    r.set(DETECTION_FRAME, frame_bytes)

                    # Acknowledge the frame
                    r.xack(FRAME_STREAM, CONSUMER_GROUP, msg_id)

                    total_detections += len(detections)

                    if detections:
                        det_summary = ", ".join(
                            f"{d['class_name']}({d['confidence']:.2f})"
                            for d in detections
                        )
                        logger.info(
                            f"Frame {frame_num}: {len(detections)} vehicle(s) "
                            f"[{det_summary}] — {inference_ms:.0f}ms"
                        )
                    elif frames_processed % 100 == 0:
                        logger.info(
                            f"Frames processed: {frames_processed}, "
                            f"total vehicles: {total_detections}"
                        )

        except redis.ConnectionError:
            logger.warning("Redis connection lost, reconnecting in 2s...")
            time.sleep(2)
        except Exception as e:
            logger.error(f"Error in detection loop: {e}", exc_info=True)
            time.sleep(1)

    logger.info(
        f"Shutting down — processed {frames_processed} frames, "
        f"detected {total_detections} vehicles total"
    )


if __name__ == "__main__":
    run()
