"""
services/recorder/recorder.py — Continuous DVR recording to NAS.

PURPOSE:
    Records the camera's RTSP sub-stream directly to 1-hour MPEG-TS segments
    on the QNAP NAS. Uses ffmpeg's segment muxer for automatic splitting.

    KEY DESIGN DECISIONS:
    - MPEG-TS (.ts) instead of MP4:  TS is a streaming container — every
      packet is self-contained. If ffmpeg is killed mid-write, the file is
      still playable up to the last written packet.  MP4 requires a "moov"
      atom written at close time; kill it early and the whole file is corrupt.
    - Segment muxer instead of -t duration:  A single ffmpeg process handles
      all segments.  No reconnection gap between hours.  If the RTSP stream
      drops, ffmpeg reconnects automatically with -reconnect flags.

    Also handles retention: deletes day-folders older than RETENTION_DAYS.

STORAGE LAYOUT:
    /recordings/{camera_id}/YYYY-MM-DD/HH-MM.ts

CONFIG (via environment variables):
    CAMERA_ID           — Camera name (default: front_door)
    RTSP_URL            — RTSP sub-stream URL
    RECORDING_DIR       — Base output directory (default: /recordings)
    SEGMENT_DURATION    — Seconds per segment (default: 3600 = 1 hour)
    RETENTION_DAYS      — Days to keep recordings (default: 28)
    CLEANUP_INTERVAL    — Hours between cleanup runs (default: 6)
"""

import os
import sys
import time
import signal
import logging
import subprocess
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CAMERA_ID = os.getenv("CAMERA_ID", "front_door")
RTSP_URL = os.getenv("RTSP_URL", "")
RECORDING_DIR = os.getenv("RECORDING_DIR", "/recordings")
SEGMENT_DURATION = int(os.getenv("SEGMENT_DURATION", "3600"))  # 1 hour
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "28"))
CLEANUP_INTERVAL_HOURS = int(os.getenv("CLEANUP_INTERVAL", "6"))
TZ_NAME = os.getenv("LOCATION_TIMEZONE", "America/Toronto")

TZ_LOCAL = ZoneInfo(TZ_NAME)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("recorder")

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
_shutdown = False
_ffmpeg_proc = None


def _handle_signal(signum, frame):
    global _shutdown
    logger.info("Shutdown signal received — stopping recording...")
    _shutdown = True
    if _ffmpeg_proc and _ffmpeg_proc.poll() is None:
        _ffmpeg_proc.terminate()


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


# ---------------------------------------------------------------------------
# Retention cleanup
# ---------------------------------------------------------------------------
def cleanup_old_recordings():
    """Delete recording day-folders older than RETENTION_DAYS."""
    camera_dir = os.path.join(RECORDING_DIR, CAMERA_ID)
    if not os.path.isdir(camera_dir):
        return

    cutoff = datetime.now(TZ_LOCAL) - timedelta(days=RETENTION_DAYS)
    cutoff_str = cutoff.strftime("%Y-%m-%d")
    removed_count = 0

    for day_folder in sorted(os.listdir(camera_dir)):
        day_path = os.path.join(camera_dir, day_folder)
        if not os.path.isdir(day_path):
            continue

        # Day folder name is YYYY-MM-DD
        if day_folder < cutoff_str:
            try:
                shutil.rmtree(day_path)
                removed_count += 1
                logger.info(f"Deleted old recordings: {day_path}")
            except Exception as e:
                logger.warning(f"Failed to delete {day_path}: {e}")

    if removed_count:
        logger.info(f"Cleanup complete — removed {removed_count} day folder(s)")
    else:
        logger.info("Cleanup complete — no old recordings to remove")


# ---------------------------------------------------------------------------
# ffmpeg segment recording
# ---------------------------------------------------------------------------
def record_segments() -> bool:
    """
    Record using ffmpeg's segment muxer with MPEG-TS output.

    A single ffmpeg process runs continuously, splitting into segment files
    automatically.  Each segment is named by its start time.

    The strftime pattern in -segment_format produces paths like:
        /recordings/front_door/2026-02-24/00-00.ts
        /recordings/front_door/2026-02-24/01-00.ts

    Returns True if recording ended normally (shutdown), False on error.
    """
    global _ffmpeg_proc

    camera_dir = os.path.join(RECORDING_DIR, CAMERA_ID)
    os.makedirs(camera_dir, exist_ok=True)

    # The segment muxer uses strftime to generate output filenames.
    # We create day-folders via a wrapper since ffmpeg can't mkdir.
    # Instead, we'll use a flat pattern and a watcher, OR we pre-create
    # today's folder and let the strftime pattern handle it.
    #
    # Actually ffmpeg segment muxer CAN use nested strftime patterns,
    # but it won't create directories.  So we pre-create today + tomorrow.
    _ensure_day_dirs(camera_dir)

    # Output pattern:  /recordings/front_door/%Y-%m-%d/%H-%M.ts
    segment_pattern = os.path.join(camera_dir, "%Y-%m-%d", "%H-%M.ts")

    safe_url = RTSP_URL.split("@")[-1] if "@" in RTSP_URL else RTSP_URL
    logger.info(f"Starting continuous recording to: {camera_dir}/")
    logger.info(f"RTSP source: {safe_url}")
    logger.info(f"Segment duration: {SEGMENT_DURATION}s, format: MPEG-TS")

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        # --- RTSP input with reconnection ---
        "-rtsp_transport", "tcp",
        "-timeout", "5000000",           # 5s I/O timeout (µs)
        "-i", RTSP_URL,
        # --- Copy codec (no transcode) ---
        "-c", "copy",
        # --- Segment muxer ---
        "-f", "segment",
        "-segment_time", str(SEGMENT_DURATION),
        "-segment_format", "mpegts",     # MPEG-TS = crash-safe
        "-segment_atclocktime", "1",     # Split on clock boundaries (top of hour)
        "-strftime", "1",               # Use strftime in output pattern
        "-reset_timestamps", "1",        # Each segment starts at t=0
        "-break_non_keyframes", "1",     # Don't wait for keyframe at split point
        segment_pattern,
    ]

    try:
        _ffmpeg_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        logger.info(f"ffmpeg started (PID {_ffmpeg_proc.pid})")

        # Monitor the process, periodically create day dirs and run cleanup
        last_cleanup = time.time()
        last_dir_check = time.time()

        while _ffmpeg_proc.poll() is None:
            if _shutdown:
                logger.info("Sending SIGTERM to ffmpeg...")
                _ffmpeg_proc.terminate()
                try:
                    _ffmpeg_proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    _ffmpeg_proc.kill()
                return True

            now = time.time()

            # Pre-create day directories every 30 minutes
            if now - last_dir_check > 1800:
                _ensure_day_dirs(camera_dir)
                last_dir_check = now

            # Run retention cleanup periodically
            if now - last_cleanup > CLEANUP_INTERVAL_HOURS * 3600:
                cleanup_old_recordings()
                last_cleanup = now

            time.sleep(2)

        # ffmpeg exited on its own — likely RTSP dropped
        rc = _ffmpeg_proc.returncode
        stderr_out = _ffmpeg_proc.stderr.read().decode(errors="replace")
        if rc != 0:
            logger.warning(f"ffmpeg exited with code {rc}: {stderr_out[-500:]}")
        else:
            logger.info("ffmpeg exited normally")
        return rc == 0

    except FileNotFoundError:
        logger.error("ffmpeg not found — install ffmpeg in the container")
        return False
    except Exception as e:
        logger.error(f"Recording error: {e}")
        return False
    finally:
        _ffmpeg_proc = None


def _ensure_day_dirs(camera_dir: str):
    """Pre-create today's and tomorrow's date directories.

    ffmpeg's segment muxer can't create directories, so we need them
    to exist before ffmpeg tries to write a file into them.
    """
    now = datetime.now(TZ_LOCAL)
    for offset in range(2):  # today + tomorrow
        day = now + timedelta(days=offset)
        day_dir = os.path.join(camera_dir, day.strftime("%Y-%m-%d"))
        os.makedirs(day_dir, exist_ok=True)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def run():
    if not RTSP_URL:
        logger.error("RTSP_URL not set — check your .env or docker-compose.yml")
        sys.exit(1)

    logger.info(f"DVR Recorder starting for camera '{CAMERA_ID}'")
    logger.info(f"Segment duration: {SEGMENT_DURATION}s ({SEGMENT_DURATION//3600}h)")
    logger.info(f"Retention: {RETENTION_DAYS} days")
    logger.info(f"Recording directory: {RECORDING_DIR}/{CAMERA_ID}/")

    # Initial cleanup
    cleanup_old_recordings()

    reconnect_delay = 5

    while not _shutdown:
        ok = record_segments()

        if not ok and not _shutdown:
            logger.warning(f"Recording failed — retrying in {reconnect_delay}s...")
            time.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 60)
        else:
            reconnect_delay = 5


    logger.info("DVR Recorder stopped")


if __name__ == "__main__":
    run()
