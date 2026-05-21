"""
routes/notifications/frame.py — JPEG frame + MP4 clip helpers backed by Redis.

`get_latest_frame` / `get_sd_frame` pull the freshest decode-ready bytes
from a camera's stream. `draw_bbox_on_frame` overlays a coloured box on
a JPEG for visual context. `build_clip` records a short MP4 from the
live frame stream and re-encodes via ffmpeg for cross-platform playback.
"""

import json

import cv2
import numpy as np

import routes as ctx

from ._shared import logger

def get_latest_frame(camera_id: str = "") -> bytes | None:
    """
    Get the latest JPEG frame from Redis.
    Tries the HD frame first (frame_hd:{camera_id}), then falls
    back to the sub-stream (frames:{camera_id}).
    Uses a SEPARATE binary Redis client (decode_responses=False)
    because frame data is raw JPEG bytes.

    Phase 9a: pass camera_id to pull from a specific camera's streams.
    Defaults to the dashboard's primary camera (env CAMERA_ID).
    """
    try:
        r_bin = ctx.r_bin
        if camera_id and camera_id != ctx.CAMERA_ID:
            # Build keys for the requested camera
            from contracts.streams import HD_FRAME_KEY as _HD_TMPL, FRAME_STREAM as _FRAME_TMPL, stream_key
            hd_key = stream_key(_HD_TMPL, camera_id=camera_id).encode()
            frame_stream = stream_key(_FRAME_TMPL, camera_id=camera_id).encode()
        else:
            hd_key = ctx.HD_FRAME_KEY.encode() if ctx.HD_FRAME_KEY else None
            frame_stream = ctx.FRAME_STREAM.encode()

        # --- Try HD frame first (clearer image) ---
        if hd_key:
            hd_bytes = r_bin.get(hd_key)
            if hd_bytes and len(hd_bytes) > 100:
                return hd_bytes

        # --- Fall back to sub-stream ---
        entries = r_bin.xrevrange(frame_stream, count=1)
        if entries:
            _, data = entries[0]
            frame = data.get(b"frame")
            if frame and len(frame) > 100:  # Sanity check — real JPEG is >100 bytes
                return frame
            logger.warning(f"Frame data too small or missing: {len(frame) if frame else 0} bytes")
    except Exception as e:
        logger.warning(f"Failed to get latest frame: {e}")
    return None


def get_sd_frame(camera_id: str = "") -> bytes | None:
    """Get the sub-stream (SD) frame only — used for bbox coordinate reference."""
    try:
        r_bin = ctx.r_bin
        if camera_id and camera_id != ctx.CAMERA_ID:
            from contracts.streams import FRAME_STREAM as _FRAME_TMPL, stream_key
            frame_stream = stream_key(_FRAME_TMPL, camera_id=camera_id).encode()
        else:
            frame_stream = ctx.FRAME_STREAM.encode()
        entries = r_bin.xrevrange(frame_stream, count=1)
        if entries:
            _, data = entries[0]
            frame = data.get(b"frame")
            if frame and len(frame) > 100:
                return frame
    except Exception:
        pass
    return None


def draw_bbox_on_frame(frame_bytes: bytes, bbox_json: str,
                       label: str = "",
                       color: tuple = (0, 255, 0)) -> bytes:
    """
    Draw a bounding box highlight on a JPEG frame.
    If the frame is HD, scales bbox coords from sub-stream dimensions.
    Returns the modified JPEG bytes.
    """
    try:
        bbox = json.loads(bbox_json) if isinstance(bbox_json, str) else bbox_json
        if not bbox or len(bbox) != 4:
            return frame_bytes

        np_arr = np.frombuffer(frame_bytes, np.uint8)
        img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        if img is None:
            return frame_bytes

        x1, y1, x2, y2 = [float(v) for v in bbox]
        snap_h, snap_w = img.shape[:2]

        # If snapshot is HD (>= 1000px wide), scale bbox from SD coords
        if snap_w >= 1000:
            sd_frame = get_sd_frame()
            if sd_frame:
                sd_arr = np.frombuffer(sd_frame, np.uint8)
                sd_img = cv2.imdecode(sd_arr, cv2.IMREAD_COLOR)
                if sd_img is not None:
                    sd_h, sd_w = sd_img.shape[:2]
                    sx = snap_w / sd_w
                    sy = snap_h / sd_h
                    x1, y1, x2, y2 = x1 * sx, y1 * sy, x2 * sx, y2 * sy

        ix1, iy1, ix2, iy2 = int(x1), int(y1), int(x2), int(y2)
        cv2.rectangle(img, (ix1, iy1), (ix2, iy2), color, 3)
        if label:
            cv2.putText(img, label, (ix1, iy1 - 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)

        _, encoded = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])
        return encoded.tobytes()
    except Exception:
        return frame_bytes


def build_clip(duration: float = 5.0, fps: int = 10, camera_id: str = "") -> bytes | None:
    """
    Capture frames from the Redis stream and encode as MP4 clip.
    Collects frames for `duration` seconds using xread to get unique frames.
    Returns MP4 bytes or None on failure.

    If `camera_id` is empty, uses the dashboard's primary FRAME_STREAM
    (ctx.FRAME_STREAM). Pass an explicit id to record from a non-primary
    camera (cam2, etc.) via the per-camera frame stream template.
    """
    import cv2
    import numpy as np
    import tempfile
    import time as _time

    try:
        # Use the shared binary-mode client (ctx.r_bin) instead of opening a
        # fresh connection per call — every Telegram clip request used to
        # leak a TCP connection to Redis, which accumulated over days.
        r_bin = ctx.r_bin
        frames = []
        target_count = int(duration * fps)
        start = _time.monotonic()
        if camera_id and camera_id != ctx.CAMERA_ID:
            from contracts.streams import FRAME_STREAM as _FRAME_TMPL, stream_key as _stream_key
            stream_key = _stream_key(_FRAME_TMPL, camera_id=camera_id).encode()
        else:
            stream_key = ctx.FRAME_STREAM.encode()

        # Get the latest stream ID as our starting point
        latest = r_bin.xrevrange(stream_key, count=1)
        if not latest:
            logger.warning("build_clip: no frames in stream")
            return None
        last_id = latest[0][0]  # Start AFTER this frame

        # Grab the first frame immediately
        _, data = latest[0]
        frame_bytes = data.get(b"frame")
        if frame_bytes and len(frame_bytes) > 100:
            nparr = np.frombuffer(frame_bytes, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img is not None:
                frames.append(img)

        # Read NEW frames using xread(block=...) — only returns genuinely new entries
        while len(frames) < target_count:
            elapsed = _time.monotonic() - start
            if elapsed > duration + 3:
                break  # Safety timeout

            # Block up to 500ms waiting for a new frame
            result = r_bin.xread({stream_key: last_id}, count=5, block=500)
            if not result:
                continue  # No new frames yet, retry

            for _, entries in result:
                for entry_id, data in entries:
                    last_id = entry_id
                    frame_bytes = data.get(b"frame")
                    if frame_bytes and len(frame_bytes) > 100:
                        nparr = np.frombuffer(frame_bytes, np.uint8)
                        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                        if img is not None:
                            frames.append(img)
                    if len(frames) >= target_count:
                        break

        if len(frames) < 5:
            logger.warning(f"build_clip: only captured {len(frames)} frames, need at least 5")
            return None

        # Calculate actual FPS from capture timing
        actual_duration = _time.monotonic() - start
        actual_fps = len(frames) / actual_duration if actual_duration > 0 else fps

        # Encode to MP4 using actual capture FPS so playback speed matches reality
        h, w = frames[0].shape[:2]
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp_path = tmp.name

        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(tmp_path, fourcc, actual_fps, (w, h))
        for f in frames:
            writer.write(f)
        writer.release()

        # Re-encode to H.264 for Telegram/browser compatibility
        # OpenCV's mp4v (MPEG-4 Part 2) won't play inline in Telegram
        import os
        import subprocess
        h264_path = tmp_path + ".h264.mp4"
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", tmp_path,
                 "-c:v", "libx264", "-preset", "ultrafast",
                 "-movflags", "+faststart", "-an", h264_path],
                capture_output=True, timeout=30,
            )
            os.unlink(tmp_path)
            with open(h264_path, "rb") as f:
                video_bytes = f.read()
            os.unlink(h264_path)
        except Exception as e:
            logger.warning(f"build_clip: ffmpeg re-encode failed ({e}), using raw mp4v")
            with open(tmp_path, "rb") as f:
                video_bytes = f.read()
            os.unlink(tmp_path)

        if len(video_bytes) < 1000:
            logger.warning(f"build_clip: video too small ({len(video_bytes)} bytes)")
            return None

        logger.info(f"build_clip: captured {len(frames)} unique frames in {actual_duration:.1f}s ({actual_fps:.1f} fps)")
        return video_bytes

    except Exception as e:
        logger.warning(f"build_clip error: {e}")
        return None
