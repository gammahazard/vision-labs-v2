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

import cv2
import numpy as np

import routes as ctx
from contracts.tz import TZ_LOCAL

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
        notify_vehicle_idle, is_configured, get_sd_frame,
    )

    from contracts.streams import (
        EVENT_STREAM as _EVT_TMPL,
        CONFIG_KEY as _CFG_TMPL,
        HD_FRAME_KEY as _HD_TMPL,
        stream_key as _stream_key,
    )

    r = ctx.r
    r_bin = ctx.r_bin
    VEHICLE_SNAPSHOT_DIR = ctx.VEHICLE_SNAPSHOT_DIR

    # Per-camera snapshots live in {SNAPSHOT_DIR}/{camera_id}/{event_id}.jpg.
    # Vehicle snapshots live in {VEHICLE_SNAPSHOT_DIR}/{camera_id}/{day}/...
    # Event journal stays flat by day; each entry carries a `camera` field.
    SNAPSHOT_DIR = os.path.join(os.environ.get("SNAPSHOT_DIR", "/data/snapshots"))
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)

    EVENT_JOURNAL_DIR = os.environ.get("EVENT_JOURNAL_DIR", "/data/events")
    os.makedirs(EVENT_JOURNAL_DIR, exist_ok=True)

    def _load_enabled_cameras() -> list:
        """Snapshot the cameras:registry hash → list of enabled camera ids."""
        try:
            raw = r.hgetall("cameras:registry") or {}
            out = []
            for cid, val in raw.items():
                try:
                    entry = json.loads(val)
                    if entry.get("enabled", True):
                        out.append(entry.get("id") or cid)
                except Exception:
                    continue
            return sorted(set(out))
        except Exception:
            return []

    def _journal_event(msg_id: str, data: dict, camera_id: str):
        """Append event to daily JSONL file for persistent audit trail.
        Includes the source camera so downstream consumers can filter."""
        try:
            ts = float(data.get("timestamp", time.time()))
            dt = datetime.fromtimestamp(ts, tz=TZ_LOCAL)
            day_str = dt.strftime("%Y-%m-%d")
            journal_path = os.path.join(EVENT_JOURNAL_DIR, f"{day_str}.jsonl")
            entry = {
                "id": msg_id if isinstance(msg_id, str) else msg_id.decode(),
                "camera": camera_id,
                "timestamp": ts,
                "time": dt.strftime("%H:%M:%S"),
                **{k: v for k, v in data.items() if k != "timestamp"},
            }
            with open(journal_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.debug(f"Event journal write failed: {e}")

    # Initial fan-out: every enabled camera's event stream gets watched.
    # Refreshed periodically so new cameras get picked up without a restart.
    cameras = _load_enabled_cameras() or [ctx.CAMERA_ID]
    last_ids: dict[str, str] = {
        _stream_key(_EVT_TMPL, camera_id=cid): "$" for cid in cameras
    }
    # Map stream name -> camera_id for routing events to the right disk dir
    stream_to_camera: dict[str, str] = {
        _stream_key(_EVT_TMPL, camera_id=cid): cid for cid in cameras
    }
    logger.info(
        f"Event poller started — watching {len(last_ids)} stream(s): "
        f"{sorted(last_ids.keys())} · snapshots → {SNAPSHOT_DIR}/<camera>/, "
        f"vehicles → {VEHICLE_SNAPSHOT_DIR}/<camera>/, journal → {EVENT_JOURNAL_DIR}"
    )

    loop = asyncio.get_event_loop()
    refresh_counter = 0  # Re-scan registry every N loop ticks

    def _save_snapshot(event_id: str, bbox_json: str = "", snapshot_key: str = "",
                       camera_id: str = ""):
        """Save a snapshot JPEG for this event to per-camera disk subdir.

        If snapshot_key is provided (set by tracker at detection time), uses
        that frame from Redis instead of the live frame. This ensures the
        snapshot matches the actual detection moment.
        Falls back to HD/sub-stream live frame for the named camera.

        Returns the RAW frame bytes (before bbox annotation) so the caller
        can forward them to the Telegram notification.
        """
        try:
            cam = camera_id or ctx.CAMERA_ID
            hd_key = _stream_key(_HD_TMPL, camera_id=cam)

            # --- Prefer tracker-saved snapshot (matches detection frame) ---
            frame = None
            is_hd = False
            sd_frame = get_sd_frame(camera_id=cam)  # bbox scaling reference
            if snapshot_key:
                frame = r_bin.get(snapshot_key.encode() if isinstance(snapshot_key, str) else snapshot_key)
                # Tracker snapshots are sub-stream (SD) resolution since
                # the bbox coords come from the sub-stream detector.
                # Do NOT set is_hd — bbox draws directly without scaling.

            # --- Fall back to live frame for this camera ---
            if not frame:
                hd_bytes = r_bin.get(hd_key.encode())
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

            # Per-camera disk subdir: /data/snapshots/{camera_id}/{event_id}.jpg
            # Old root-dir snapshots are left in place for backward compat;
            # consumers read from camera subdir first, falling back to root.
            safe_id = event_id.replace(":", "-")
            cam_dir = os.path.join(SNAPSHOT_DIR, cam)
            os.makedirs(cam_dir, exist_ok=True)
            path = os.path.join(cam_dir, f"{safe_id}.jpg")
            with open(path, "wb") as f:
                f.write(frame)

            return raw_frame
        except Exception as e:
            logger.debug(f"Snapshot save failed for {event_id}: {e}")
            return None

    def _save_vehicle_snapshot(snapshot_key: str, event_data: dict, camera_id: str = ""):
        """
        Pull vehicle snapshot JPEG from Redis and save to disk.
        Draws bbox highlight if available. Per-camera layout:
            {VEHICLE_SNAPSHOT_DIR}/{camera_id}/{YYYY-MM-DD}/{HH-MM-SS}_{class}.jpg
        """
        cam = camera_id or ctx.CAMERA_ID
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
            dt = datetime.fromtimestamp(ts, tz=TZ_LOCAL)
            day_str = dt.strftime("%Y-%m-%d")
            time_str = dt.strftime("%H-%M-%S")

            # Per-camera layout: /data/snapshots/vehicles/{camera_id}/{day}/{time_class}.jpg
            day_dir = os.path.join(VEHICLE_SNAPSHOT_DIR, cam, day_str)
            os.makedirs(day_dir, exist_ok=True)
            path = os.path.join(day_dir, f"{time_str}_{vehicle_class}.jpg")
            with open(path, "wb") as f:
                f.write(jpeg_data)

            logger.debug(f"Vehicle snapshot saved: {path}")
        except Exception as e:
            logger.debug(f"Vehicle snapshot save failed: {e}")

    while True:
        try:
            # Periodically re-scan the registry so new cameras get added live.
            refresh_counter += 1
            if refresh_counter >= 30:  # ~ every minute given 2s xread block
                refresh_counter = 0
                current_cams = _load_enabled_cameras()
                current_streams = {
                    _stream_key(_EVT_TMPL, camera_id=cid): cid for cid in current_cams
                }
                added = set(current_streams) - set(last_ids)
                removed = set(last_ids) - set(current_streams)
                for stream in added:
                    last_ids[stream] = "$"
                    stream_to_camera[stream] = current_streams[stream]
                    logger.info(f"Event poller: now watching {stream}")
                for stream in removed:
                    last_ids.pop(stream, None)
                    stream_to_camera.pop(stream, None)
                    logger.info(f"Event poller: stopped watching {stream}")

            if not last_ids:
                await asyncio.sleep(2)
                continue

            # Run blocking multi-stream xread in a thread
            entries = await loop.run_in_executor(
                None, lambda: r.xread(dict(last_ids), count=10, block=2000)
            )
            if entries:
                for stream_name, messages in entries:
                    # Normalize stream name to str for dict lookup
                    sname = stream_name if isinstance(stream_name, str) else stream_name.decode()
                    src_camera = stream_to_camera.get(sname, ctx.CAMERA_ID)
                    # Per-camera notification config
                    cfg_key = _stream_key(_CFG_TMPL, camera_id=src_camera)
                    cfg = r.hgetall(cfg_key)
                    notify_person = cfg.get("notify_person", "1") == "1"
                    notify_vehicle = cfg.get("notify_vehicle", "1") == "1"
                    suppress_known = cfg.get("suppress_known", "0") == "1"

                    for msg_id, data in messages:
                        last_ids[sname] = msg_id
                        # Inject camera id into data so notify_* / consumers see it
                        data = {**data, "camera_id": src_camera}
                        event_type = data.get("event_type", "")

                        # Journal ALL events to daily JSONL (with camera tag)
                        await loop.run_in_executor(
                            None, _journal_event, msg_id, data, src_camera
                        )

                        if event_type == "person_appeared":
                            bbox_json = data.get("snapshot_bbox", "") or data.get("bbox", "")
                            evt_snap_key = data.get("snapshot_key", "")
                            snap_bytes = await loop.run_in_executor(
                                None,
                                lambda eid=msg_id, bb=bbox_json, sk=evt_snap_key, c=src_camera:
                                    _save_snapshot(eid, bb, sk, c)
                            )
                            if is_configured() and notify_person:
                                await notify_person_detected(
                                    data, event_id=msg_id,
                                    snapshot_bytes=snap_bytes,
                                )

                        elif event_type == "person_identified":
                            bbox_json = data.get("snapshot_bbox", "") or data.get("bbox", "")
                            evt_snap_key = data.get("snapshot_key", "")
                            snap_bytes = await loop.run_in_executor(
                                None,
                                lambda eid=msg_id, bb=bbox_json, sk=evt_snap_key, c=src_camera:
                                    _save_snapshot(eid, bb, sk, c)
                            )
                            if is_configured() and notify_person and not suppress_known:
                                await notify_person_identified(
                                    data, event_id=msg_id,
                                    snapshot_bytes=snap_bytes,
                                )

                        elif event_type == "vehicle_detected":
                            bbox_json = data.get("bbox", "")
                            evt_snap_key = data.get("snapshot_key", "")
                            await loop.run_in_executor(
                                None,
                                lambda eid=msg_id, bb=bbox_json, sk=evt_snap_key, c=src_camera:
                                    _save_snapshot(eid, bb, sk, c)
                            )
                            snapshot_key = data.get("snapshot_key", "")
                            if snapshot_key:
                                await loop.run_in_executor(
                                    None, _save_vehicle_snapshot,
                                    snapshot_key, data, src_camera,
                                )

                        elif event_type == "vehicle_idle":
                            bbox_json = data.get("bbox", "")
                            evt_snap_key = data.get("snapshot_key", "")
                            snap_bytes = await loop.run_in_executor(
                                None,
                                lambda eid=msg_id, bb=bbox_json, sk=evt_snap_key, c=src_camera:
                                    _save_snapshot(eid, bb, sk, c)
                            )
                            snapshot_key = data.get("snapshot_key", "")
                            if snapshot_key:
                                await loop.run_in_executor(
                                    None, _save_vehicle_snapshot,
                                    snapshot_key, data, src_camera,
                                )
                            if is_configured() and notify_vehicle:
                                await notify_vehicle_idle(
                                    data, event_id=msg_id,
                                    snapshot_bytes=snap_bytes,
                                )

                        elif event_type in (
                            "stream_stale", "stream_recovered",
                            "recorder_error", "recorder_recovered",
                        ):
                            # System-health alerts always fire (no notify_*
                            # toggle to opt out — if your camera is dead or
                            # DVR is failing you want to know). Generic
                            # Telegram broadcast — bypasses per-event-type
                            # config gating.
                            if is_configured():
                                try:
                                    from routes.notifications import broadcast_text, _esc
                                    cam_name = src_camera
                                    try:
                                        import cameras as _cam_reg
                                        for c in _cam_reg.list_enabled_cameras():
                                            if c.get("id") == src_camera:
                                                cam_name = c.get("name") or src_camera
                                                break
                                    except Exception:
                                        pass
                                    reason = data.get("reason", "")
                                    cam_line = f"• Camera: {_esc(cam_name)} ({_esc(src_camera)})"
                                    if event_type == "stream_stale":
                                        msg = (
                                            f"\U0001f4f6 <b>Camera Stream Stale</b>\n"
                                            f"{cam_line}\n"
                                            f"• Reason: {_esc(reason or 'no frames')}\n"
                                            f"• The camera may be offline or the network dropped."
                                        )
                                    elif event_type == "stream_recovered":
                                        msg = (
                                            f"✅ <b>Camera Stream Recovered</b>\n"
                                            f"{cam_line}\n"
                                            f"• Frames are flowing again."
                                        )
                                    elif event_type == "recorder_error":
                                        msg = (
                                            f"\U0001f4be <b>DVR Recorder Failing</b>\n"
                                            f"{cam_line}\n"
                                            f"• Reason: {_esc(reason or 'ffmpeg keeps crashing')}\n"
                                            f"• Recordings on this camera may be incomplete."
                                        )
                                    else:  # recorder_recovered
                                        msg = (
                                            f"✅ <b>DVR Recorder Recovered</b>\n"
                                            f"{cam_line}\n"
                                            f"• {_esc(reason or 'recording stable')}"
                                        )
                                    await broadcast_text(msg)
                                except Exception as e:
                                    logger.warning(f"System-health broadcast failed: {e}")

        except Exception as e:
            logger.warning(f"Event notification poller error: {e}")
            await asyncio.sleep(5)

        await asyncio.sleep(0.1)
