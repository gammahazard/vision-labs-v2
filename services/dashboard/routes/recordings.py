"""
DVR Recording Playback Routes

Provides API endpoints for browsing and streaming recorded .ts segments
from the camera DVR (recorder service). Extracted from video_pipeline.py
so DVR functionality remains after video pipeline removal.
"""

import asyncio
import os
import logging
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

logger = logging.getLogger("vision-labs.recordings")

router = APIRouter()

RECORDINGS_DIR = Path("/data/recordings")
DEFAULT_CAMERA = os.getenv("CAMERA_ID", "cam1")

# Cap on the remux cache (`/tmp/rec-cache`). Without this the cache grew
# unbounded — every distinct .ts segment ever played stayed as an .mp4
# forever, eventually filling the container's tmpfs. Eviction is LRU by
# mtime when the cache exceeds the cap.
REC_CACHE_DIR = Path("/tmp/rec-cache")
REC_CACHE_MAX_BYTES = int(os.getenv("REC_CACHE_MAX_BYTES", str(5 * 1024 * 1024 * 1024)))  # 5 GB


def _evict_rec_cache_if_full() -> None:
    """LRU eviction by mtime when the cache exceeds REC_CACHE_MAX_BYTES."""
    try:
        if not REC_CACHE_DIR.is_dir():
            return
        files = [
            (p, p.stat().st_size, p.stat().st_mtime)
            for p in REC_CACHE_DIR.iterdir()
            if p.is_file() and p.suffix == ".mp4"
        ]
        total = sum(s for _p, s, _m in files)
        if total <= REC_CACHE_MAX_BYTES:
            return
        # Drop oldest-mtime first until we're under the cap with some slack.
        target = int(REC_CACHE_MAX_BYTES * 0.8)
        files.sort(key=lambda t: t[2])  # oldest first
        for p, size, _mtime in files:
            if total <= target:
                break
            try:
                p.unlink()
                total -= size
                logger.info(f"rec-cache evicted {p.name} ({size//1024//1024} MB)")
            except OSError:
                continue
    except Exception as e:
        logger.warning(f"rec-cache eviction failed: {e}")


def _resolve_camera(camera: str) -> str:
    """Map empty/whitespace camera arg to the dashboard's default. Sanitizes
    so the value can't escape the recordings dir."""
    cam = (camera or "").strip()
    if not cam:
        return DEFAULT_CAMERA
    # Only allow alnum + underscore-dash (camera ids in registry follow this)
    safe = "".join(c for c in cam if c.isalnum() or c in "-_")
    return safe or DEFAULT_CAMERA


@router.get("/api/recordings/cameras")
async def list_recording_cameras():
    """List every camera that has any recordings on disk.
    Used by the DVR tab to populate the camera selector."""
    if not RECORDINGS_DIR.is_dir():
        return {"cameras": []}
    out = []
    for entry in sorted(RECORDINGS_DIR.iterdir()):
        if not entry.is_dir():
            continue
        # Count date subdirs as a proxy for "has any recordings"
        days = [d for d in entry.iterdir() if d.is_dir() and len(d.name) == 10]
        out.append({"id": entry.name, "day_count": len(days)})
    return {"cameras": out}


@router.get("/api/recordings/dates")
async def list_recording_dates(camera: str = ""):
    """List available recording dates (day folders) for a camera."""
    cam = _resolve_camera(camera)
    camera_dir = RECORDINGS_DIR / cam
    if not camera_dir.is_dir():
        return {"dates": [], "camera": cam, "error": "No recordings directory found"}

    dates = sorted(
        [d.name for d in camera_dir.iterdir() if d.is_dir() and len(d.name) == 10],
        reverse=True,
    )
    return {"dates": dates, "camera": cam}


@router.get("/api/recordings/segments")
async def list_recording_segments(date: str = "", camera: str = ""):
    """List .ts segments for a given camera+date."""
    cam = _resolve_camera(camera)
    if not date or len(date) != 10:
        return {"segments": [], "camera": cam, "error": "Provide a valid date (YYYY-MM-DD)"}

    safe_date = "".join(c for c in date if c in "0123456789-")
    day_dir = RECORDINGS_DIR / cam / safe_date

    if not day_dir.is_dir():
        return {"segments": [], "camera": cam, "error": f"No recordings for {date}"}

    segments = []
    for f in sorted(day_dir.iterdir()):
        if f.suffix.lower() == ".ts" and f.is_file():
            # Filename like "14-00.ts" means 2:00 PM
            time_label = f.stem.replace("-", ":")
            try:
                hour = int(f.stem.split("-")[0])
                minute = int(f.stem.split("-")[1]) if "-" in f.stem else 0
                ampm = "AM" if hour < 12 else "PM"
                display_hour = hour % 12 or 12
                time_label = f"{display_hour}:{minute:02d} {ampm}"
            except (ValueError, IndexError):
                pass

            segments.append({
                "filename": f.name,
                "time": time_label,
                "size_mb": round(f.stat().st_size / (1024 * 1024), 1),
            })

    return {"date": date, "camera": cam, "segments": segments}


@router.get("/api/recordings/stream/{date}/{segment}")
async def stream_recording(date: str, segment: str, camera: str = ""):
    """Stream a .ts recording remuxed to MP4 for browser playback.
    Pass ?camera=<id> to pick the right per-camera dir; defaults to primary."""
    cam = _resolve_camera(camera)
    safe_date = "".join(c for c in date if c in "0123456789-")
    safe_segment = "".join(c for c in segment if c.isalnum() or c in "-_.")

    file_path = RECORDINGS_DIR / cam / safe_date / safe_segment
    if not file_path.is_file():
        return JSONResponse({"error": "Recording not found"}, status_code=404)

    # Cache remuxed MP4 in /tmp so repeated plays are instant.
    REC_CACHE_DIR.mkdir(exist_ok=True)
    # Use `/` as a path separator inside the filename via `__` so the
    # mp4 name can't accidentally collide when one camera+date prefix
    # extends into another. (cam="a", date="b_c") vs (cam="a_b", date="c")
    # used to map to the same file with a single `_`.
    mp4_name = f"{cam}__{safe_date}__{safe_segment.replace('.ts', '.mp4')}"
    mp4_path = REC_CACHE_DIR / mp4_name

    # Only re-encode if not already cached (or source is newer)
    if not mp4_path.exists() or mp4_path.stat().st_mtime < file_path.stat().st_mtime:
        # Run ffmpeg as a child process via asyncio so a slow remux
        # doesn't block other requests on the FastAPI event loop.
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y",
            "-hide_banner", "-loglevel", "error",
            "-i", str(file_path),
            "-c:v", "libx264",       # Re-encode for browser compatibility
            "-preset", "ultrafast",   # Speed over compression
            "-crf", "28",             # Reasonable quality for security cam
            "-pix_fmt", "yuv420p",    # Universal pixel format
            "-r", "15",              # Fix framerate to clean 15fps
            "-an",                    # No audio track
            "-movflags", "+faststart",
            "-f", "mp4",
            str(mp4_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning(f"ffmpeg remux timed out for {file_path}")
            return JSONResponse({"error": "Remux timed out"}, status_code=504)
        if proc.returncode != 0:
            logger.warning(f"ffmpeg encode failed: {stderr.decode(errors='ignore')[:200]}")
            return JSONResponse({"error": "Failed to convert recording"}, status_code=500)
        # Best-effort cache trim after a successful encode.
        _evict_rec_cache_if_full()

    return FileResponse(
        str(mp4_path),
        media_type="video/mp4",
        headers={"Accept-Ranges": "bytes"},
    )
