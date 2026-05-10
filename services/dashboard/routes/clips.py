"""
routes/clips.py — Clip gallery API endpoints.

Handles listing, serving, and deleting generated video clips
stored in /data/clips/ with JSON metadata sidecars.
"""

import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

logger = logging.getLogger("dashboard.clips")
router = APIRouter()

CLIPS_DIR = Path("/data/clips")


@router.get("/api/video/clips")
async def list_clips(limit: int = 50, offset: int = 0):
    """List generated clips with metadata, newest first."""
    if not CLIPS_DIR.exists():
        return {"clips": [], "total": 0}

    # Collect all .mp4 files sorted newest first
    all_clips = sorted(CLIPS_DIR.glob("*.mp4"), key=lambda f: f.stat().st_mtime, reverse=True)
    total = len(all_clips)
    page = all_clips[offset:offset + limit]

    clips = []
    for f in page:
        meta = {}
        meta_path = f.with_suffix(".json")
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
            except Exception:
                pass

        try:
            stat = f.stat()
            clips.append({
                "filename": f.name,
                "size_mb": round(stat.st_size / (1024 * 1024), 1),
                "created": stat.st_mtime,
                "prompt": meta.get("prompt", ""),
                "negative": meta.get("negative", ""),
                "wan_model": meta.get("wan_model", ""),
                "resolution": meta.get("resolution", ""),
                "seed": meta.get("seed", ""),
                "num_frames": meta.get("num_frames", ""),
            })
        except OSError:
            continue

    return {"clips": clips, "total": total}


@router.get("/api/video/clips/{filename}")
async def serve_clip(filename: str):
    """Serve a clip video file."""
    path = CLIPS_DIR / filename
    if not path.exists() or not path.is_file():
        return JSONResponse(status_code=404, content={"error": "Clip not found"})
    return FileResponse(str(path), media_type="video/mp4", filename=filename)


@router.delete("/api/video/clips/{filename}")
async def delete_clip(filename: str):
    """Delete a clip and its metadata sidecar."""
    path = CLIPS_DIR / filename
    meta_path = path.with_suffix(".json")

    if not path.exists():
        return JSONResponse(status_code=404, content={"error": "Clip not found"})

    try:
        path.unlink()
        if meta_path.exists():
            meta_path.unlink()
        return {"status": "deleted", "filename": filename}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
