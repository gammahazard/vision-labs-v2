"""
routes/browse.py — Snapshot browser + enrolled faces gallery.

PURPOSE:
    Browse vehicle detection snapshots organized by day, and view
    enrolled face photos in a unified gallery.

ENDPOINTS:
    GET  /api/browse/days            — List day folders (reverse chronological)
    GET  /api/browse/days/{date}     — List snapshot files for a date (YYYY-MM-DD)
    GET  /api/browse/snapshot/{path} — Serve a snapshot JPEG from disk
    GET  /api/browse/faces           — List enrolled faces with photo URLs
"""

import os
from datetime import datetime

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response
import httpx

import routes as ctx

router = APIRouter(prefix="/api/browse", tags=["browse"])


@router.get("/days")
async def list_days():
    """
    List available day folders containing vehicle snapshots.
    Returns [{date, count, path}] sorted newest-first.
    """
    base = ctx.VEHICLE_SNAPSHOT_DIR
    if not os.path.isdir(base):
        return []

    days = []
    try:
        for name in sorted(os.listdir(base), reverse=True):
            day_path = os.path.join(base, name)
            if os.path.isdir(day_path):
                # Count JPEG files in this day folder
                count = sum(1 for f in os.listdir(day_path) if f.lower().endswith(".jpg"))
                days.append({"date": name, "count": count})
    except Exception as e:
        ctx.logger.warning(f"Browse days error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})

    return days


@router.get("/days/{date}")
async def list_day_snapshots(date: str):
    """
    List snapshot files for a specific day.
    Returns [{filename, timestamp, vehicle_class, url}] sorted newest-first.
    """
    # Validate date format to prevent path traversal
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "Invalid date format, use YYYY-MM-DD"})

    day_path = os.path.join(ctx.VEHICLE_SNAPSHOT_DIR, date)
    if not os.path.isdir(day_path):
        return []

    snapshots = []
    try:
        for fname in sorted(os.listdir(day_path), reverse=True):
            if not fname.lower().endswith(".jpg"):
                continue

            # Parse filename: HH-MM-SS_classname.jpg
            base_name = fname.rsplit(".", 1)[0]  # strip .jpg
            parts = base_name.split("_", 1)
            time_str = parts[0] if parts else ""
            vehicle_class = parts[1] if len(parts) > 1 else "vehicle"

            snapshots.append({
                "filename": fname,
                "time": time_str.replace("-", ":"),
                "vehicle_class": vehicle_class,
                "url": f"/api/browse/snapshot/{date}/{fname}",
            })
    except Exception as e:
        ctx.logger.warning(f"Browse day {date} error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})

    return snapshots


@router.get("/snapshot/{date}/{filename}")
async def serve_snapshot(date: str, filename: str):
    """
    Serve a specific vehicle snapshot JPEG from disk.
    Date and filename are validated to prevent path traversal.
    """
    # Validate date format
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "Invalid date"})

    # Prevent path traversal in filename
    safe_name = os.path.basename(filename)
    path = os.path.join(ctx.VEHICLE_SNAPSHOT_DIR, date, safe_name)

    if not os.path.isfile(path):
        return JSONResponse(status_code=404, content={"error": "Snapshot not found"})

    with open(path, "rb") as f:
        data = f.read()

    return Response(content=data, media_type="image/jpeg")


@router.get("/faces")
async def list_faces_for_browse():
    """
    List enrolled faces with photo URLs for the browse gallery.
    Proxies the face-recognizer service (same as routes/faces.py).
    """
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{ctx.FACE_API_URL}/api/faces", timeout=5)
            data = resp.json()
            # Face-recognizer may return {"faces": [...]} or a plain list
            raw = data.get("faces", data) if isinstance(data, dict) else data
            if not isinstance(raw, list):
                return []

            # Group by person name — each angle becomes a photo in the group
            grouped: dict = {}
            for face in raw:
                name = face.get("name") or face.get("label") or "Unknown"
                fid = face.get("id") or face.get("face_id")
                if name not in grouped:
                    grouped[name] = {
                        "name": name,
                        "photo_url": f"/api/faces/{fid}/photo" if fid else "",
                        "angles": [],
                        "sighting_count": face.get("sighting_count", 0),
                    }
                if fid:
                    grouped[name]["angles"].append({
                        "id": fid,
                        "photo_url": f"/api/faces/{fid}/photo",
                    })
                # Keep highest sighting count
                sc = face.get("sighting_count", 0)
                if sc and sc > grouped[name].get("sighting_count", 0):
                    grouped[name]["sighting_count"] = sc

            return list(grouped.values())
    except Exception as e:
        ctx.logger.warning(f"Browse faces error: {e}")
        return JSONResponse(status_code=503, content={"error": "Face recognizer not available"})
