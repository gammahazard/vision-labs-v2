"""tracker/core/main.py — entrypoint: signal handler, consumer-group setup, main loop."""

import json
import signal
import time

import redis
from contracts.redis_client import make_redis_client

from .config import (
    logger,
    CAMERA_ID,
    REDIS_HOST,
    REDIS_PORT,
    IOU_THRESHOLD,
    LOST_TIMEOUT,
    CONSUMER_GROUP,
    VEHICLE_CONSUMER_GROUP,
    CONSUMER_NAME,
    DETECTION_STREAM,
    EVENT_STREAM,
    STATE_KEY,
    VEHICLE_STREAM,
    CONFIG_KEY,
    CONFIG_RELOAD_INTERVAL,
)
from .manager import PersonTracker

_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    logger.info("Shutdown signal received...")
    _shutdown = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


def setup_consumer_group(r: redis.Redis) -> None:
    """Create consumer groups for detection and vehicle streams."""
    for stream, group in [
        (DETECTION_STREAM, CONSUMER_GROUP),
        (VEHICLE_STREAM, VEHICLE_CONSUMER_GROUP),
    ]:
        try:
            r.xgroup_create(stream, group, id="$", mkstream=True)
            logger.info(f"Created consumer group '{group}' on '{stream}'")
        except redis.ResponseError as e:
            if "BUSYGROUP" in str(e):
                logger.info(f"Consumer group '{group}' already exists")
            else:
                raise


def run():
    """
    Main loop: read detections from Redis → update tracker → publish events.

    The tracker is a lightweight CPU service — no GPU needed. It just does
    bounding box math and state management.

    Reads from two streams:
    - DETECTION_STREAM (person detections from pose-detector)
    - VEHICLE_STREAM (vehicle detections from vehicle-detector)
    """
    logger.info(f"Starting tracker for camera '{CAMERA_ID}'")
    logger.info(f"Reading from: {DETECTION_STREAM} + {VEHICLE_STREAM}")
    logger.info(f"Publishing to: {EVENT_STREAM}")
    logger.info(f"State key: {STATE_KEY}")
    logger.info(f"IoU threshold: {IOU_THRESHOLD}, Lost timeout: {LOST_TIMEOUT}s")

    # Connect to Redis
    r = make_redis_client(decode_responses=False, host=REDIS_HOST, port=REDIS_PORT)
    r.ping()
    logger.info("Redis connection verified")

    # Setup consumer groups (person + vehicle)
    setup_consumer_group(r)

    # Initialize tracker
    tracker = PersonTracker(r, IOU_THRESHOLD, LOST_TIMEOUT)
    messages_processed = 0

    while not _shutdown:
        try:
            # Read from both person and vehicle detection streams
            messages = r.xreadgroup(
                CONSUMER_GROUP,
                CONSUMER_NAME,
                {DETECTION_STREAM: ">"},
                count=1,
                block=500,  # Shorter block so we can check vehicles too
            )

            # Also check for vehicle detections — non-blocking poll.
            # NOTE: `block=` MUST be omitted (or set to None). `block=0` means
            # "block forever" in Redis XREADGROUP, which used to deadlock the
            # tracker on cameras that have no vehicle producer (detect_vehicles=false)
            # because the vehicle stream stays permanently empty for them.
            vehicle_messages = r.xreadgroup(
                VEHICLE_CONSUMER_GROUP,
                CONSUMER_NAME,
                {VEHICLE_STREAM: ">"},
                count=1,
            )
        except redis.ConnectionError:
            logger.warning("Redis connection lost — retrying...")
            time.sleep(1)
            continue

        # --- Process person detections ---
        if not messages:
            # Even with no new detections, check for lost people
            tracker.update([], time.time())
        else:
            for stream_name, entries in messages:
                for message_id, data in entries:
                    timestamp = float(data.get(b"timestamp", b"0").decode())
                    detections_json = data.get(b"detections", b"[]").decode()
                    detections = json.loads(detections_json)

                    # Hot-reload IoU and lost timeout from Redis config (set by dashboard)
                    messages_processed += 1
                    if messages_processed % CONFIG_RELOAD_INTERVAL == 0:
                        try:
                            cfg_iou = r.hget(CONFIG_KEY, "iou_threshold")
                            cfg_timeout = r.hget(CONFIG_KEY, "lost_timeout")
                            cfg_vidle = r.hget(CONFIG_KEY, "vehicle_idle_timeout")
                            cfg_suppress = r.hget(CONFIG_KEY, "suppress_known")
                            if cfg_iou:
                                new_iou = float(cfg_iou)
                                if new_iou != tracker.iou_threshold:
                                    logger.info(f"Config updated: IoU {tracker.iou_threshold} → {new_iou}")
                                    tracker.iou_threshold = new_iou
                            if cfg_timeout:
                                new_timeout = float(cfg_timeout)
                                if new_timeout != tracker.lost_timeout:
                                    logger.info(f"Config updated: lost_timeout {tracker.lost_timeout} → {new_timeout}")
                                    tracker.lost_timeout = new_timeout
                            if cfg_vidle:
                                new_vidle = float(cfg_vidle)
                                if new_vidle != tracker.vehicle_idle_timeout:
                                    logger.info(f"Config updated: vehicle_idle_timeout {tracker.vehicle_idle_timeout} → {new_vidle}")
                                    tracker.vehicle_idle_timeout = new_vidle
                            if cfg_suppress is not None:
                                new_suppress = cfg_suppress in ("1", b"1")
                                if new_suppress != tracker.suppress_known:
                                    logger.info(f"Config updated: suppress_known {tracker.suppress_known} → {new_suppress}")
                                    tracker.suppress_known = new_suppress
                        except (ValueError, redis.ConnectionError):
                            pass

                    # Update frame dimensions from detection metadata
                    fw = data.get(b"frame_width", b"").decode()
                    fh = data.get(b"frame_height", b"").decode()
                    if fw and fh:
                        tracker.frame_width = int(fw)
                        tracker.frame_height = int(fh)

                    # Update tracker with new detections
                    tracker.update(detections, timestamp)

                    # Acknowledge message
                    r.xack(DETECTION_STREAM, CONSUMER_GROUP, message_id)

        # --- Process vehicle detections ---
        if vehicle_messages:
            for stream_name, entries in vehicle_messages:
                for message_id, data in entries:
                    timestamp = float(data.get(b"timestamp", b"0").decode())
                    detections_json = data.get(b"detections", b"[]").decode()
                    detections = json.loads(detections_json)
                    frame_bytes = data.get(b"frame_bytes", None)

                    if detections:
                        tracker._process_vehicle_detections(detections, timestamp, frame_bytes)

                    r.xack(VEHICLE_STREAM, VEHICLE_CONSUMER_GROUP, message_id)

    logger.info(
        f"Tracker stopped. Total events emitted: {tracker.total_events}"
    )
