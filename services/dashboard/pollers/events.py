"""
services/dashboard/pollers/events.py — event stream consumer + Telegram broadcaster.

PURPOSE:
    Continuously consume `events:{camera_id}` from Redis and for each event:
      1. Journal it to /data/events/YYYY-MM-DD.jsonl (always)
      2. Save a snapshot JPEG to /data/snapshots/<event_id>.jpg (always)
      3. For vehicle events, also save to /data/snapshots/vehicles/YYYY-MM-DD/HH-MM-SS_class.jpg
      4. If Telegram is configured + the per-type cooldown allows + not suppressed:
         broadcast a photo with bbox highlight + AI scene description
         to every approved user.

RELATIONSHIPS:
    - Reads from: events:{camera_id} stream (Redis), config:{camera_id} hash (Redis)
    - Pulls saved snapshots from: person_snapshot:* and vehicle_snapshot:* Redis keys
    - Falls back to: frame_hd:* or latest frame from frames:* if snapshot expired
    - Writes to: /data/snapshots/*.jpg, /data/snapshots/vehicles/<day>/*.jpg,
                 /data/events/<day>.jsonl
    - Calls: routes.notifications.notify_person_detected/_identified/_vehicle_idle

WHY THE BLOCKING xread RUNS IN A THREAD EXECUTOR:
    r.xread(block=2000) blocks the current asyncio task for up to 2s waiting
    for new events. Running it in `loop.run_in_executor()` keeps the asyncio
    event loop free so the WebSocket frame streaming doesn't stutter.

WHY NESTED FUNCTIONS:
    _journal_event, _save_snapshot, _save_vehicle_snapshot close over local
    variables like SNAPSHOT_DIR, EVENT_JOURNAL_DIR. Keeping them nested
    preserves that idiom and matches the pre-extraction code exactly.
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import cv2
import numpy as np

import routes as ctx

logger = logging.getLogger("dashboard.event_poller")


async def event_notification_poller():
    """
    Background task: poll the event stream for new events.
    Two responsibilities:
      1. ALWAYS save a camera snapshot for person_appeared events (for the event feed)
      2. Optionally send Telegram notifications (when configured)

    IMPORTANT: r.xread(block=...) is a synchronous blocking call.
    We run it in a thread executor to avoid blocking the asyncio event loop,
    which would starve the WebSocket frame streaming.
    """
    from routes.notifications import (
        notify_person_detected, notify_person_identified,
        notify_vehicle_idle, is_configured, get_latest_frame, get_sd_frame,
    )

    r = ctx.r
    r_bin = ctx.r_bin
    EVENT_STREAM = ctx.EVENT_STREAM
    CONFIG_KEY = ctx.CONFIG_KEY
    HD_FRAME_KEY = ctx.HD_FRAME_KEY
    VEHICLE_SNAPSHOT_DIR = ctx.VEHICLE_SNAPSHOT_DIR

    # Ensure snapshot directory exists
    SNAPSHOT_DIR = os.path.join(os.environ.get("SNAPSHOT_DIR", "/data/snapshots"))
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)

    # Event journal directory (daily JSONL files)
    EVENT_JOURNAL_DIR = os.environ.get("EVENT_JOURNAL_DIR", "/data/events")
    os.makedirs(EVENT_JOURNAL_DIR, exist_ok=True)

    def _journal_event(msg_id: str, data: dict):
        """Append event to daily JSONL file for persistent audit trail."""
        try:
            _tz = ZoneInfo(os.getenv("LOCATION_TIMEZONE", "America/Toronto"))
            ts = float(data.get("timestamp", time.time()))
            dt = datetime.fromtimestamp(ts, tz=_tz)
            day_str = dt.strftime("%Y-%m-%d")
            journal_path = os.path.join(EVENT_JOURNAL_DIR, f"{day_str}.jsonl")
            entry = {
                "id": msg_id if isinstance(msg_id, str) else msg_id.decode(),
                "timestamp": ts,
                "time": dt.strftime("%H:%M:%S"),
                **{k: v for k, v in data.items() if k != "timestamp"},
            }
            with open(journal_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.debug(f"Event journal write failed: {e}")

    last_id = "$"  # Only process new events from this point forward
    logger.info(f"Event poller started — snapshots → {SNAPSHOT_DIR}, vehicles → {VEHICLE_SNAPSHOT_DIR}, journal → {EVENT_JOURNAL_DIR}")

    loop = asyncio.get_event_loop()

    def _save_snapshot(event_id: str, bbox_json: str = "", snapshot_key: str = ""):
        """Save a snapshot JPEG for this event.
        If snapshot_key is provided (set by tracker at detection time), uses
        that frame from Redis instead of the live frame. This ensures the
        snapshot matches the actual detection moment.
        Falls back to HD/sub-stream live frame if no snapshot_key.

        Returns the RAW frame bytes (before bbox annotation) so the caller
        can forward them to the Telegram notification.
        """
        try:
            # --- Prefer tracker-saved snapshot (matches detection frame) ---
            frame = None
            is_hd = False
            sd_frame = get_sd_frame()  # Always needed for bbox scaling reference
            if snapshot_key:
                frame = r_bin.get(snapshot_key.encode() if isinstance(snapshot_key, str) else snapshot_key)
                # Tracker snapshots are sub-stream (SD) resolution since
                # the bbox coords come from the sub-stream detector.
                # Do NOT set is_hd — bbox draws directly without scaling.

            # --- Fall back to live frame ---
            if not frame:
                hd_bytes = r_bin.get(HD_FRAME_KEY.encode())
                frame = hd_bytes if hd_bytes else sd_frame
                is_hd = bool(hd_bytes)

            if not frame:
                return None

            # Keep a copy of the raw frame for the notification
            raw_frame = frame

            # Draw bbox highlight if provided
            if bbox_json:
                try:
                    bbox = json.loads(bbox_json) if isinstance(bbox_json, str) else bbox_json
                    if len(bbox) == 4:
                        np_arr = np.frombuffer(frame, np.uint8)
                        img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
                        if img is not None:
                            x1, y1, x2, y2 = [float(v) for v in bbox]

                            # Scale bbox from sub-stream coords to snapshot
                            # resolution if we're using the HD frame
                            if is_hd and sd_frame:
                                sd_arr = np.frombuffer(sd_frame, np.uint8)
                                sd_img = cv2.imdecode(sd_arr, cv2.IMREAD_COLOR)
                                if sd_img is not None:
                                    sd_h, sd_w = sd_img.shape[:2]
                                    hd_h, hd_w = img.shape[:2]
                                    sx = hd_w / sd_w
                                    sy = hd_h / sd_h
                                    x1, y1, x2, y2 = x1*sx, y1*sy, x2*sx, y2*sy

                            ix1, iy1, ix2, iy2 = int(x1), int(y1), int(x2), int(y2)
                            # Draw thick bright cyan rectangle
                            cv2.rectangle(img, (ix1, iy1), (ix2, iy2), (255, 200, 0), 3)
                            # Add small label
                            cv2.putText(img, "DETECTION", (ix1, iy1 - 8),
                                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 200, 0), 2)
                            _, frame = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
                            frame = frame.tobytes()
                except Exception:
                    pass  # Fall back to raw frame

            # Redis event IDs contain ":" — replace for safe filenames
            safe_id = event_id.replace(":", "-")
            path = os.path.join(SNAPSHOT_DIR, f"{safe_id}.jpg")
            with open(path, "wb") as f:
                f.write(frame)

            return raw_frame
        except Exception as e:
            logger.debug(f"Snapshot save failed for {event_id}: {e}")
            return None

    def _save_vehicle_snapshot(snapshot_key: str, event_data: dict):
        """
        Pull vehicle snapshot JPEG from Redis and save to disk.
        Draws bbox highlight if available. Organized as:
        vehicles/YYYY-MM-DD/HH-MM-SS_class.jpg
        """
        try:
            jpeg_data = r_bin.get(snapshot_key.encode() if isinstance(snapshot_key, str) else snapshot_key)
            if not jpeg_data:
                return

            # Draw bbox highlight if present in event data
            # Prefer snapshot_bbox (bbox at capture time) over bbox (latest position)
            bbox_json = event_data.get("snapshot_bbox", "") or event_data.get("bbox", "")
            vehicle_class = event_data.get("vehicle_class", "vehicle")
            if bbox_json:
                try:
                    bbox = json.loads(bbox_json) if isinstance(bbox_json, str) else bbox_json
                    if len(bbox) == 4:
                        np_arr = np.frombuffer(jpeg_data, np.uint8)
                        img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
                        if img is not None:
                            x1, y1, x2, y2 = [int(v) for v in bbox]
                            # Orange to match live overlay vehicle color
                            cv2.rectangle(img, (x1, y1), (x2, y2), (0, 165, 255), 3)
                            label = vehicle_class.upper()
                            cv2.putText(img, label, (x1, y1 - 8),
                                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 165, 255), 2)
                            _, jpeg_data = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
                            jpeg_data = jpeg_data.tobytes()
                except Exception:
                    pass  # Fall back to raw frame

            # Parse timestamp from event data
            ts = float(event_data.get("timestamp", time.time()))
            _tz = ZoneInfo(os.getenv("LOCATION_TIMEZONE", "America/Toronto"))
            dt = datetime.fromtimestamp(ts, tz=_tz)
            day_str = dt.strftime("%Y-%m-%d")
            time_str = dt.strftime("%H-%M-%S")

            # Create day folder and write file
            day_dir = os.path.join(VEHICLE_SNAPSHOT_DIR, day_str)
            os.makedirs(day_dir, exist_ok=True)
            path = os.path.join(day_dir, f"{time_str}_{vehicle_class}.jpg")
            with open(path, "wb") as f:
                f.write(jpeg_data)

            logger.debug(f"Vehicle snapshot saved: {path}")
        except Exception as e:
            logger.debug(f"Vehicle snapshot save failed: {e}")

    while True:
        try:
            # Run blocking xread in a thread so we don't block the event loop
            entries = await loop.run_in_executor(
                None, lambda: r.xread({EVENT_STREAM: last_id}, count=10, block=2000)
            )
            if entries:
                # Read notification preferences from Redis config
                cfg = r.hgetall(CONFIG_KEY)
                notify_person = cfg.get("notify_person", "1") == "1"
                notify_vehicle = cfg.get("notify_vehicle", "1") == "1"
                suppress_known = cfg.get("suppress_known", "0") == "1"

                for stream_name, messages in entries:
                    for msg_id, data in messages:
                        last_id = msg_id
                        event_type = data.get("event_type", "")

                        # Journal ALL events to daily JSONL
                        await loop.run_in_executor(
                            None, _journal_event, msg_id, data
                        )

                        if event_type == "person_appeared":
                            # Use snapshot_bbox (matches the saved snapshot frame)
                            # instead of live bbox to avoid bbox/frame mismatch
                            bbox_json = data.get("snapshot_bbox", "") or data.get("bbox", "")
                            evt_snap_key = data.get("snapshot_key", "")
                            snap_bytes = await loop.run_in_executor(
                                None, lambda eid=msg_id, bb=bbox_json, sk=evt_snap_key: _save_snapshot(eid, bb, sk)
                            )
                            # Send Telegram if person notifications enabled
                            if is_configured() and notify_person:
                                await notify_person_detected(
                                    data, event_id=msg_id,
                                    snapshot_bytes=snap_bytes,
                                )

                        elif event_type == "person_identified":
                            # Use snapshot_bbox to match saved frame
                            bbox_json = data.get("snapshot_bbox", "") or data.get("bbox", "")
                            evt_snap_key = data.get("snapshot_key", "")
                            snap_bytes = await loop.run_in_executor(
                                None, lambda eid=msg_id, bb=bbox_json, sk=evt_snap_key: _save_snapshot(eid, bb, sk)
                            )
                            # Skip if suppress_known is on (known people don't alert)
                            if is_configured() and notify_person and not suppress_known:
                                await notify_person_identified(
                                    data, event_id=msg_id,
                                    snapshot_bytes=snap_bytes,
                                )

                        elif event_type == "vehicle_detected":
                            # Save event snapshot with highlighted bbox for event detail modal
                            bbox_json = data.get("bbox", "")
                            evt_snap_key = data.get("snapshot_key", "")
                            await loop.run_in_executor(
                                None, lambda eid=msg_id, bb=bbox_json, sk=evt_snap_key: _save_snapshot(eid, bb, sk)
                            )
                            # Also save vehicle snapshot to disk in day folder
                            snapshot_key = data.get("snapshot_key", "")
                            if snapshot_key:
                                await loop.run_in_executor(
                                    None, _save_vehicle_snapshot, snapshot_key, data
                                )

                        elif event_type == "vehicle_idle":
                            # Save snapshot with highlighted bbox for feedback modal
                            bbox_json = data.get("bbox", "")
                            evt_snap_key = data.get("snapshot_key", "")
                            snap_bytes = await loop.run_in_executor(
                                None, lambda eid=msg_id, bb=bbox_json, sk=evt_snap_key: _save_snapshot(eid, bb, sk)
                            )
                            # Save vehicle snapshot to disk too
                            snapshot_key = data.get("snapshot_key", "")
                            if snapshot_key:
                                await loop.run_in_executor(
                                    None, _save_vehicle_snapshot, snapshot_key, data
                                )
                            if is_configured() and notify_vehicle:
                                await notify_vehicle_idle(
                                    data, event_id=msg_id,
                                    snapshot_bytes=snap_bytes,
                                )

        except Exception as e:
            logger.warning(f"Event notification poller error: {e}")
            await asyncio.sleep(5)

        await asyncio.sleep(0.1)
