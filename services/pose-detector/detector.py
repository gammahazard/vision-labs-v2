"""
services/pose-detector/detector.py — Runs YOLOv8 pose detection on camera frames.

PURPOSE:
    This is the "eyes" of the system. It reads raw JPEG frames from Redis,
    runs YOLOv8s-pose inference on the GPU, and publishes structured detection
    results (bounding boxes + body keypoints) back to Redis.

    It does NOT track people across frames — that's the tracker's job.
    It only answers: "What do I see in THIS frame?"

RELATIONSHIPS:
    - Reads from: Redis Stream "frames:{camera_id}" (published by camera-ingester)
    - Writes to: Redis Stream "detections:pose:{camera_id}" (consumed by tracker)
    - Stream keys defined in: contracts/streams.py
    - Model: YOLOv8s-pose (~500 MB VRAM on RTX 3090)

DATA FLOW:
    camera-ingester → [frames:front_door] → THIS SERVICE → [detections:pose:front_door] → tracker

CONFIG (environment variables):
    CAMERA_ID          — Which camera's frames to process (default: "front_door")
    REDIS_HOST         — Redis server hostname (default: "127.0.0.1")
    REDIS_PORT         — Redis server port (default: 6379)
    MODEL_NAME         — YOLO model to use (default: "yolov8s-pose.pt")
    CONFIDENCE_THRESH  — Minimum detection confidence (default: 0.5)
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
MODEL_NAME = os.getenv("MODEL_NAME", "yolov8s-pose.pt")
CONFIDENCE_THRESH = float(os.getenv("CONFIDENCE_THRESH", "0.5"))
MIN_KEYPOINTS = int(os.getenv("MIN_KEYPOINTS", "3"))           # Min visible body keypoints to accept
KP_CONFIDENCE_THRESH = float(os.getenv("KP_CONFIDENCE_THRESH", "0.3"))  # Keypoint visibility threshold
CONSUMER_GROUP = os.getenv("CONSUMER_GROUP", "pose_detectors")
CONSUMER_NAME = os.getenv("CONSUMER_NAME", "detector_1")

# Stream keys — resolved from contracts/streams.py
FRAME_STREAM = stream_key(_FRAME_TMPL, camera_id=CAMERA_ID)
DETECTION_STREAM = stream_key(_DET_TMPL, detector_type="pose", camera_id=CAMERA_ID)
CONFIG_KEY = stream_key(_CFG_TMPL, camera_id=CAMERA_ID)
DETECTION_FRAME = stream_key(_DET_FRAME_TMPL, detector_type="pose", camera_id=CAMERA_ID)

# Max detections to keep in the output stream
MAX_DETECTION_STREAM_LEN = 1000

# How often to check Redis for config changes (every N frames)
CONFIG_RELOAD_INTERVAL = 25

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("pose-detector")

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
    Load the YOLOv8 pose model.

    On first run, ultralytics auto-downloads the model weights (~25 MB for yolov8s-pose).
    The model runs on GPU automatically if CUDA is available.
    """
    logger.info(f"Loading YOLO model: {model_name}")
    model = YOLO(model_name)

    # Log device info — check CUDA availability via torch (more reliable than model.device)
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
    each frame is processed by exactly ONE detector in the group. This is
    how you scale: spin up a second detector container and they auto-balance.

    The '$' means "start reading from new messages only" (don't reprocess old frames).
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
# Frame Decoding
# ---------------------------------------------------------------------------
def decode_frame(frame_bytes: bytes) -> np.ndarray:
    """
    Decode JPEG bytes back into an OpenCV image (numpy array).

    The camera-ingester encoded frames as JPEG before publishing to Redis.
    We decode them here so YOLO can process them.
    """
    np_arr = np.frombuffer(frame_bytes, np.uint8)
    frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
    return frame


# ---------------------------------------------------------------------------
# Detection Formatting
# ---------------------------------------------------------------------------
def format_detections(results, min_keypoints: int = MIN_KEYPOINTS,
                      kp_conf_thresh: float = KP_CONFIDENCE_THRESH) -> list[dict]:
    """
    Convert YOLO results into a clean list of detection dictionaries.

    Each detection contains:
    - bbox: [x1, y1, x2, y2] bounding box coordinates
    - confidence: float (0-1)
    - class_name: str (always "person" for pose model)
    - keypoints: list of [x, y, confidence] for 17 body points

    The 17 COCO keypoints are:
    0: nose, 1: left_eye, 2: right_eye, 3: left_ear, 4: right_ear,
    5: left_shoulder, 6: right_shoulder, 7: left_elbow, 8: right_elbow,
    9: left_wrist, 10: right_wrist, 11: left_hip, 12: right_hip,
    13: left_knee, 14: right_knee, 15: left_ankle, 16: right_ankle

    Keypoint quality filter:
    If the model provides keypoints, we require at least `min_keypoints`
    body keypoints (indices 5-16: shoulders through ankles) to have
    confidence >= kp_conf_thresh. This eliminates false positives from
    objects that look person-shaped (lamp posts, bushes, shadows) but
    lack plausible body structure.
    """
    detections = []

    for result in results:
        if result.boxes is None or len(result.boxes) == 0:
            continue

        boxes = result.boxes
        keypoints = result.keypoints if result.keypoints is not None else None

        for i in range(len(boxes)):
            # Bounding box
            bbox = boxes.xyxy[i].cpu().numpy().tolist()  # [x1, y1, x2, y2]
            confidence = float(boxes.conf[i].cpu().numpy())
            class_id = int(boxes.cls[i].cpu().numpy())
            class_name = result.names[class_id]

            # Only keep person detections
            if class_name != "person":
                continue

            detection = {
                "bbox": [round(v, 1) for v in bbox],
                "confidence": round(confidence, 3),
                "class_name": class_name,
            }

            # Add keypoints if available (pose model)
            if keypoints is not None and i < len(keypoints):
                kps = keypoints[i].data.cpu().numpy().tolist()
                # kps shape: [17, 3] — each keypoint is [x, y, confidence]
                kp_list = [
                    [round(v, 1) for v in kp] for kp in kps[0]
                ]
                detection["keypoints"] = kp_list

                # --- Keypoint quality filter ---
                # Check body keypoints only (indices 5-16: shoulders → ankles)
                # Skip face points (0-4) since they're often occluded
                body_kps = kp_list[5:]  # 12 body keypoints
                visible_count = sum(
                    1 for kp in body_kps if kp[2] >= kp_conf_thresh
                )
                if visible_count < min_keypoints:
                    # Not enough visible body keypoints — likely a false positive
                    continue

            detections.append(detection)

    return detections


# ---------------------------------------------------------------------------
# Main Detection Loop
# ---------------------------------------------------------------------------
def run():
    """
    Main loop: read frames from Redis → run YOLO → publish detections.

    Uses Redis consumer groups so frames are distributed across detector
    instances. Each frame is acknowledged after processing so it won't
    be re-delivered if the detector restarts.
    """
    logger.info(f"Starting pose detector for camera '{CAMERA_ID}'")
    logger.info(f"Reading from: {FRAME_STREAM}")
    logger.info(f"Publishing to: {DETECTION_STREAM}")

    # Connect to Redis
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=False)
    r.ping()
    logger.info("Redis connection verified")

    # Set up consumer group
    setup_consumer_group(r)

    # Load YOLO model
    model = load_model(MODEL_NAME)

    # Tracking metrics
    frames_processed = 0
    total_inference_time = 0.0
    last_log_time = time.time()
    current_confidence = CONFIDENCE_THRESH
    current_min_keypoints = MIN_KEYPOINTS
    current_kp_confidence = KP_CONFIDENCE_THRESH

    logger.info(f"Keypoint quality filter: min_keypoints={current_min_keypoints}, "
                f"kp_confidence>={current_kp_confidence}")

    while not _shutdown:
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

        # Read next frame from the consumer group
        # Block for up to 1 second waiting for new frames
        try:
            messages = r.xreadgroup(
                CONSUMER_GROUP,
                CONSUMER_NAME,
                {FRAME_STREAM: ">"},  # ">" means undelivered messages only
                count=1,
                block=1000,  # Block 1s if no frames available
            )
        except redis.ConnectionError:
            logger.warning("Redis connection lost — retrying...")
            time.sleep(1)
            continue

        if not messages:
            continue  # No frames available, loop back

        # Process each frame
        for stream_name, entries in messages:
            for message_id, data in entries:
                # Decode the JPEG frame
                frame_bytes = data[b"frame"]
                frame_number = data.get(b"frame_number", b"0").decode()
                timestamp = data.get(b"timestamp", b"0").decode()

                frame = decode_frame(frame_bytes)
                if frame is None:
                    logger.warning(f"Failed to decode frame #{frame_number}")
                    r.xack(FRAME_STREAM, CONSUMER_GROUP, message_id)
                    continue

                # Hot-reload confidence + keypoint thresholds from Redis config
                if frames_processed % CONFIG_RELOAD_INTERVAL == 0:
                    try:
                        cfg = r.hget(CONFIG_KEY, "confidence_thresh")
                        if cfg:
                            new_conf = float(cfg)
                            if new_conf != current_confidence:
                                logger.info(f"Config updated: confidence {current_confidence} → {new_conf}")
                                current_confidence = new_conf
                        kp_cfg = r.hget(CONFIG_KEY, "min_keypoints")
                        if kp_cfg:
                            new_kp = int(kp_cfg)
                            if new_kp != current_min_keypoints:
                                logger.info(f"Config updated: min_keypoints {current_min_keypoints} → {new_kp}")
                                current_min_keypoints = new_kp
                        kpc_cfg = r.hget(CONFIG_KEY, "kp_confidence_thresh")
                        if kpc_cfg:
                            new_kpc = float(kpc_cfg)
                            if new_kpc != current_kp_confidence:
                                logger.info(f"Config updated: kp_confidence {current_kp_confidence} → {new_kpc}")
                                current_kp_confidence = new_kpc
                    except (ValueError, redis.ConnectionError):
                        pass  # Keep current value on error

                # Run YOLO inference
                t_start = time.time()
                results = model(frame, conf=current_confidence, verbose=False)
                inference_time = time.time() - t_start

                # Format detections (with keypoint quality filter)
                detections = format_detections(
                    results,
                    min_keypoints=current_min_keypoints,
                    kp_conf_thresh=current_kp_confidence,
                )

                # Publish detections to Redis
                detection_data = {
                    "camera_id": CAMERA_ID,
                    "detector_type": "pose",
                    "timestamp": timestamp,
                    "frame_number": frame_number,
                    "inference_ms": str(round(inference_time * 1000, 1)),
                    "num_detections": str(len(detections)),
                    "detections": json.dumps(detections),
                    "frame_width": str(frame.shape[1]),
                    "frame_height": str(frame.shape[0]),
                }

                r.xadd(
                    DETECTION_STREAM,
                    detection_data,
                    maxlen=MAX_DETECTION_STREAM_LEN,
                )

                # Cache the source frame so the dashboard can draw bboxes on
                # the exact frame they were computed from (prevents drift)
                r.set(DETECTION_FRAME, frame_bytes)

                # Acknowledge the frame (won't be re-delivered)
                r.xack(FRAME_STREAM, CONSUMER_GROUP, message_id)

                # Update metrics
                frames_processed += 1
                total_inference_time += inference_time

                # Log progress every 10 seconds
                now = time.time()
                if now - last_log_time >= 10:
                    avg_ms = (total_inference_time / frames_processed * 1000) if frames_processed > 0 else 0
                    logger.info(
                        f"Processed {frames_processed} frames | "
                        f"Avg inference: {avg_ms:.1f}ms | "
                        f"Last frame: {len(detections)} person(s) detected"
                    )
                    last_log_time = now

    logger.info(f"Detector stopped. Total frames processed: {frames_processed}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    run()
