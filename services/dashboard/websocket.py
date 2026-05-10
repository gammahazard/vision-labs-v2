"""
services/dashboard/websocket.py — live frame + detection-overlay WebSocket.

PURPOSE:
    Serves /ws/live: streams JPEG frames (with cv2-drawn bbox/keypoint/zone
    overlays) to the browser at the configured target FPS. Handles HD/SD
    stream-mode switching, dead-zone filtering, sticky-identity labels, and
    hot-reload of the render rate from Redis config.

REGISTRATION:
    server.py calls `register(app)` once at startup. We don't use a decorator
    here because `app` is defined in server.py — keeping the registration
    explicit avoids circular imports.

AUTH:
    HTTP middleware doesn't intercept WebSocket upgrades, so we validate the
    `vl_session` cookie inside the handler and close with code 4401 if invalid.

PER-CONNECTION STATE (was the sticky-identity bug):
    `sticky_identities` and `zone_cache` are LOCAL VARIABLES inside the
    handler — i.e. each WebSocket connection gets its own. The pre-refactor
    code stored these as function attributes (`websocket_live._sticky_…`)
    which were SHARED across all connections, so two browser tabs would
    corrupt each other's labels. Fixed as a free side effect of this
    extraction.

RELATIONSHIPS:
    - Reads: detections:pose, detection_frame:pose, frames, frame_hd,
             identity_state, state, zones, config, detections:vehicle (all
             via routes.ctx for the dynamically-set keys).
    - Validates cookies via: routes.auth.validate_session
    - Uses: helpers.geometry.bbox_iou + in_dead_zone for overlay math.
"""

import asyncio
import base64
import json
import logging
import time

import cv2
import numpy as np
import redis
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

import routes as ctx
from helpers.geometry import bbox_iou, in_dead_zone

logger = logging.getLogger("dashboard.websocket")


def register(app: FastAPI):
    """Register the /ws/live WebSocket route on the given FastAPI app."""

    @app.websocket("/ws/live")
    async def websocket_live(ws: WebSocket):
        """
        Stream live camera frames with detection overlays to the browser.

        The browser receives:
        - Base64-encoded JPEG frame with bounding boxes drawn on it
        - Detection metadata (person count, person IDs, etc.)
        - Current state (who's in frame right now)

        We read the LATEST frame and its matching detection from Redis,
        draw overlays, encode as JPEG, and send to the browser.

        AUTH: HTTP middleware doesn't intercept WebSocket upgrades, so we have
        to validate the session cookie here ourselves. Without this, anyone on
        TCP 8080 can stream the camera feed without logging in.
        """
        from routes.auth import validate_session

        # Resolve all the Redis keys and clients from the route context.
        # These are set by server.py at startup before this module is registered.
        r = ctx.r
        REDIS_HOST = r.connection_pool.connection_kwargs.get("host", "redis")
        REDIS_PORT = r.connection_pool.connection_kwargs.get("port", 6379)

        # Phase 8b: WebSocket can target any camera via ?camera=<id> query param.
        # Without it, falls back to the dashboard's primary CAMERA_ID env (front_door).
        # All Redis keys are rebuilt for the requested camera so multi-camera grid
        # tiles can each open their own WebSocket connection.
        from contracts.streams import (
            FRAME_STREAM as _FRAME_TMPL,
            DETECTION_STREAM as _DET_TMPL,
            STATE_KEY as _STATE_TMPL,
            CONFIG_KEY as _CFG_TMPL,
            IDENTITY_KEY as _IDKEY_TMPL,
            ZONE_KEY as _ZONE_TMPL,
            HD_FRAME_KEY as _HD_TMPL,
            VEHICLE_STREAM as _VEH_DET_TMPL,
            DETECTION_FRAME_KEY as _DET_FRAME_TMPL,
            stream_key,
        )
        camera_id = ws.query_params.get("camera", ctx.CAMERA_ID)
        FRAME_STREAM = stream_key(_FRAME_TMPL, camera_id=camera_id)
        DETECTION_STREAM = stream_key(_DET_TMPL, detector_type="pose", camera_id=camera_id)
        DETECTION_FRAME_POSE = stream_key(_DET_FRAME_TMPL, detector_type="pose", camera_id=camera_id)
        HD_FRAME_KEY = stream_key(_HD_TMPL, camera_id=camera_id)
        IDENTITY_KEY = stream_key(_IDKEY_TMPL, camera_id=camera_id)
        STATE_KEY = stream_key(_STATE_TMPL, camera_id=camera_id)
        ZONE_KEY = stream_key(_ZONE_TMPL, camera_id=camera_id)
        VEHICLE_DET_STREAM = stream_key(_VEH_DET_TMPL, camera_id=camera_id)
        CONFIG_KEY = stream_key(_CFG_TMPL, camera_id=camera_id)

        # Validate session cookie BEFORE accepting the connection. We have to
        # accept() first to send a close frame; close-before-accept is unreliable
        # across ASGI servers. The connection closes immediately on bad auth.
        await ws.accept()
        token = ws.cookies.get("vl_session")
        username = validate_session(token) if token else None
        if not username:
            logger.warning(f"WebSocket auth rejected from {ws.client.host if ws.client else '?'} — no/invalid session")
            await ws.close(code=4401, reason="Unauthorized")
            return
        logger.info(f"WebSocket client connected (user={username}, camera={camera_id})")

        # Use a separate Redis connection for binary frame data
        r_bin = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=False)

        last_frame_id = "$"  # Start from latest

        # Dashboard render rate is now driven by the same `target_fps` Redis config
        # the ingester respects, so the slider in the UI is the single source of
        # truth for end-to-end FPS. Re-checked every 25 frames inside the loop.
        def _read_target_fps() -> float:
            try:
                raw = r.hget(CONFIG_KEY, "target_fps")
                if raw:
                    v = float(raw)
                    if v > 0:
                        return v
            except Exception:
                pass
            return 5.0  # safe default — matches DEFAULT_CONFIG

        target_fps = _read_target_fps()
        frame_interval = 1.0 / target_fps
        fps_poll_counter = 0

        # Stream mode — "sd" (default, with overlays) or "hd" (raw main stream)
        stream_mode = "sd"

        # --- Per-connection caches (was the sticky-identity bug) ---
        # These used to be function attributes shared across all WebSocket
        # connections, which corrupted labels when two browser tabs were open.
        # Local variables = each connection has its own.
        sticky_identities = {}  # person_id → name
        zone_cache = {}
        zone_cache_time = 0.0

        try:
            while True:
                loop_start = time.time()

                # Hot-reload target_fps so the dashboard slider updates render rate
                # in real time. Cheap HGET every 25 frames (~5s at 5 FPS).
                fps_poll_counter += 1
                if fps_poll_counter >= 25:
                    fps_poll_counter = 0
                    new_fps = _read_target_fps()
                    if abs(new_fps - target_fps) > 0.01:
                        target_fps = new_fps
                        frame_interval = 1.0 / target_fps
                        logger.info(f"WebSocket render rate updated → {target_fps} FPS")

                # --- Check for incoming messages (non-blocking) ---
                try:
                    msg_raw = await asyncio.wait_for(ws.receive_text(), timeout=0.001)
                    try:
                        msg = json.loads(msg_raw)
                        if msg.get("action") == "switch_stream":
                            new_mode = msg.get("stream", "sd")
                            if new_mode in ("sd", "hd"):
                                stream_mode = new_mode
                                logger.info(f"WebSocket stream mode: {stream_mode}")
                                await ws.send_json({"type": "stream_mode", "mode": stream_mode})
                    except json.JSONDecodeError:
                        pass
                except asyncio.TimeoutError:
                    pass

                try:
                    # === HD MODE: serve raw high-res frame from Redis key ===
                    if stream_mode == "hd":
                        hd_bytes = r_bin.get(HD_FRAME_KEY)
                        if not hd_bytes:
                            # No HD frame available — fall back briefly
                            await asyncio.sleep(0.1)
                            continue

                        frame_b64 = base64.b64encode(hd_bytes).decode("ascii")
                        await ws.send_json({
                            "type": "frame",
                            "frame": frame_b64,
                            "frame_number": "0",
                            "num_detections": 0,
                            "inference_ms": "--",
                            "num_people": "--",
                            "timestamp": time.time(),
                            "hd": True,
                        })

                        elapsed = time.time() - loop_start
                        await asyncio.sleep(max(0, frame_interval - elapsed))
                        continue

                    # === SD MODE: normal frame with detection overlays ===

                    # Get the latest detection
                    detections_raw = r_bin.xrevrange(
                        DETECTION_STREAM.encode(), count=1
                    )
                    detections = []
                    inference_ms = "0"
                    if detections_raw:
                        det_data = detections_raw[0][1]
                        det_json = det_data.get(b"detections", b"[]").decode()
                        detections = json.loads(det_json)
                        inference_ms = det_data.get(b"inference_ms", b"0").decode()

                    # Read the exact frame the detector processed (synced with bboxes)
                    frame_bytes = r_bin.get(DETECTION_FRAME_POSE.encode())
                    if not frame_bytes:
                        # Fallback: no detection frame yet (startup), use latest from stream
                        frames = r_bin.xrevrange(FRAME_STREAM, count=1)
                        if not frames:
                            await asyncio.sleep(0.1)
                            continue
                        frame_bytes = frames[0][1][b"frame"]
                    frame_number = "0"  # Not tracked for synced frames

                    # Decode the JPEG frame to draw overlays
                    np_arr = np.frombuffer(frame_bytes, np.uint8)
                    frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)

                    if frame is None:
                        await asyncio.sleep(0.1)
                        continue

                    # Get identity labels from face recognizer
                    identity_names = []
                    try:
                        id_state = r.hgetall(IDENTITY_KEY)
                        if id_state:
                            id_json = id_state.get("identities", "[]")
                            identity_names = json.loads(id_json)
                    except Exception:
                        pass

                    # Get tracker state for action labels and person IDs.
                    # state:* is a hash written by tracker with HSET; "people" is the JSON list.
                    tracker_persons = []
                    try:
                        state = r.hgetall(STATE_KEY)
                        if state:
                            tracker_persons = json.loads(state.get("people", "[]"))
                    except Exception:
                        pass

                    # --- Sticky Identity Logic (per-connection cache) ---
                    # Once a face is identified, stick the name to that person's bbox
                    # until they leave the frame entirely.
                    # Update sticky cache with any new identifications this frame
                    for ident in identity_names:
                        id_bbox = ident.get("bbox", [])
                        id_name = ident.get("name", "Unknown")
                        if id_name == "Unknown" or len(id_bbox) != 4:
                            continue
                        # Match identity bbox to a tracker person via IoU
                        for tp in tracker_persons:
                            tp_bbox = tp.get("bbox", [])
                            tp_pid = tp.get("person_id", "")
                            if len(tp_bbox) == 4 and tp_pid:
                                iou = bbox_iou(id_bbox, tp_bbox)
                                if iou > 0.2:
                                    sticky_identities[tp_pid] = id_name
                                    break

                    # Prune sticky identities for persons no longer tracked
                    active_pids = {tp.get("person_id", "") for tp in tracker_persons}
                    for pid in list(sticky_identities.keys()):
                        if pid not in active_pids:
                            del sticky_identities[pid]

                    # Load zone cache (per-connection) for dead zone filtering
                    now_ts = time.time()
                    if now_ts - zone_cache_time > 5:
                        raw = r.hgetall(ZONE_KEY)
                        zone_cache = {
                            k: json.loads(v) for k, v in raw.items()
                        } if raw else {}
                        zone_cache_time = now_ts

                    h, w = frame.shape[:2]

                    # Draw bounding boxes and labels on the frame
                    for det in detections:
                        bbox = det.get("bbox", [])
                        conf = det.get("confidence", 0)
                        if len(bbox) == 4:
                            x1, y1, x2, y2 = [int(v) for v in bbox]

                            # Skip drawing if bbox center is inside a dead zone
                            if in_dead_zone([x1, y1, x2, y2], w, h, zone_cache):
                                continue

                            # Match detection bbox to a tracker person for ID + action
                            person_name = None
                            action = ""
                            for tp in tracker_persons:
                                tp_bbox = tp.get("bbox", [])
                                if len(tp_bbox) == 4:
                                    iou = bbox_iou(
                                        [float(v) for v in tp_bbox],
                                        [float(x1), float(y1), float(x2), float(y2)]
                                    )
                                    if iou > 0.3:
                                        action = tp.get("action", "")
                                        tp_pid = tp.get("person_id", "")
                                        # Check sticky identity cache
                                        if tp_pid in sticky_identities:
                                            person_name = sticky_identities[tp_pid]
                                        break

                            # If no sticky identity, check live identity this frame
                            if not person_name:
                                for ident in identity_names:
                                    id_bbox = ident.get("bbox", [])
                                    if len(id_bbox) == 4:
                                        iou = bbox_iou(
                                            [float(v) for v in id_bbox],
                                            [float(x1), float(y1), float(x2), float(y2)]
                                        )
                                        if iou > 0.3:
                                            person_name = ident.get("name", "Unknown")
                                            break

                            # Color: cyan for identified, green for unknown
                            if person_name and person_name != "Unknown":
                                color = (255, 200, 0)  # Cyan (BGR)
                                label = f"{person_name} {conf:.0%}"
                            else:
                                color = (0, 255, 0)  # Green
                                label = f"Person {conf:.0%}"

                            # Append action if available
                            if action and action not in ("unknown", ""):
                                label += f" · {action}"

                            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
                            label_size = cv2.getTextSize(
                                label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2
                            )[0]
                            # Background rectangle for label
                            cv2.rectangle(
                                frame,
                                (x1, y1 - label_size[1] - 10),
                                (x1 + label_size[0] + 4, y1),
                                color,
                                -1,
                            )
                            cv2.putText(
                                frame,
                                label,
                                (x1 + 2, y1 - 5),
                                cv2.FONT_HERSHEY_SIMPLEX,
                                0.6,
                                (0, 0, 0),
                                2,
                            )

                            # Draw keypoints if available
                            keypoints = det.get("keypoints", [])
                            for kp in keypoints:
                                if len(kp) >= 3 and kp[2] > 0.3:  # Confidence > 30%
                                    cx, cy = int(kp[0]), int(kp[1])
                                    cv2.circle(frame, (cx, cy), 3, (0, 200, 255), -1)

                    # Draw vehicle bounding boxes (orange)
                    try:
                        veh_raw = r_bin.xrevrange(
                            VEHICLE_DET_STREAM.encode(), count=1
                        )
                        if veh_raw:
                            veh_data = veh_raw[0][1]
                            veh_json = veh_data.get(b"detections", b"[]").decode()
                            veh_detections = json.loads(veh_json)
                            for vdet in veh_detections:
                                vbbox = vdet.get("bbox", [])
                                vconf = vdet.get("confidence", 0)
                                vclass = vdet.get("class_name", "vehicle")
                                if len(vbbox) == 4:
                                    vx1, vy1, vx2, vy2 = [int(v) for v in vbbox]

                                    # Skip drawing if bbox center is in a dead zone
                                    if in_dead_zone([vx1, vy1, vx2, vy2], w, h, zone_cache):
                                        continue

                                    vcolor = (0, 140, 255)  # Orange (BGR)
                                    vlabel = f"{vclass} {vconf:.0%}"
                                    # Wider + thinner box: pad horizontally, use thin lines
                                    pad_x = 6
                                    cv2.rectangle(frame, (vx1 - pad_x, vy1), (vx2 + pad_x, vy2), vcolor, 1)
                                    vlabel_size = cv2.getTextSize(
                                        vlabel, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1
                                    )[0]
                                    cv2.rectangle(
                                        frame,
                                        (vx1 - pad_x, vy1 - vlabel_size[1] - 8),
                                        (vx1 - pad_x + vlabel_size[0] + 4, vy1),
                                        vcolor,
                                        -1,
                                    )
                                    cv2.putText(
                                        frame,
                                        vlabel,
                                        (vx1 - pad_x + 2, vy1 - 4),
                                        cv2.FONT_HERSHEY_SIMPLEX,
                                        0.5,
                                        (0, 0, 0),
                                        1,
                                    )
                    except Exception:
                        pass  # Vehicle stream may not be available

                    # Draw zone overlays on the frame
                    try:
                        for zone_id, zone in zone_cache.items():
                            pts_norm = zone.get("points", [])
                            if len(pts_norm) < 3:
                                continue

                            # Convert normalized coords to pixel coords
                            pts = np.array(
                                [[int(p[0] * w), int(p[1] * h)] for p in pts_norm],
                                dtype=np.int32,
                            )

                            # Zone color by alert level (BGR)
                            alert_level = zone.get("alert_level", "log_only")
                            zone_colors = {
                                "always": (0, 0, 220),       # Red
                                "night_only": (0, 140, 255),  # Orange
                                "log_only": (200, 160, 60),   # Blue
                                "ignore": (100, 100, 100),    # Gray
                                "dead_zone": (40, 40, 40),    # Dark gray/black
                            }
                            color = zone_colors.get(alert_level, (200, 160, 60))

                            # Semi-transparent fill
                            overlay = frame.copy()
                            cv2.fillPoly(overlay, [pts], color)
                            cv2.addWeighted(overlay, 0.15, frame, 0.85, 0, frame)

                            # Zone border
                            cv2.polylines(frame, [pts], True, color, 2)

                            # Zone name label
                            name = zone.get("name", zone_id)
                            cx = int(np.mean(pts[:, 0]))
                            cy = int(np.mean(pts[:, 1]))
                            label_size = cv2.getTextSize(
                                name, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1
                            )[0]
                            cv2.rectangle(
                                frame,
                                (cx - label_size[0] // 2 - 4, cy - label_size[1] // 2 - 4),
                                (cx + label_size[0] // 2 + 4, cy + label_size[1] // 2 + 4),
                                color,
                                -1,
                            )
                            cv2.putText(
                                frame,
                                name,
                                (cx - label_size[0] // 2, cy + label_size[1] // 2),
                                cv2.FONT_HERSHEY_SIMPLEX,
                                0.5,
                                (255, 255, 255),
                                1,
                            )
                    except Exception as e:
                        logger.debug(f"Zone overlay error: {e}")

                    # Encode frame back to JPEG for sending
                    _, jpeg_buf = cv2.imencode(
                        ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85]
                    )

                    # Get current state
                    state = r.hgetall(STATE_KEY)

                    # Send frame + metadata as JSON
                    frame_b64 = base64.b64encode(jpeg_buf.tobytes()).decode("ascii")

                    message = {
                        "type": "frame",
                        "frame": frame_b64,
                        "frame_number": frame_number,
                        "num_detections": len(detections),
                        "inference_ms": inference_ms,
                        "num_people": state.get("num_people", "0"),
                        "timestamp": time.time(),
                    }

                    await ws.send_json(message)

                except redis.ConnectionError:
                    logger.warning("Redis connection lost in WebSocket loop")
                    await asyncio.sleep(1)
                    continue

                # Throttle to target FPS
                elapsed = time.time() - loop_start
                sleep_time = max(0, frame_interval - elapsed)
                await asyncio.sleep(sleep_time)

        except WebSocketDisconnect:
            logger.info("WebSocket client disconnected")
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
