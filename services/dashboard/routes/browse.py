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

import json
import os
import re
from datetime import datetime

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse, Response
import httpx

import routes as ctx

router = APIRouter(prefix="/api/browse", tags=["browse"])


# Vehicle snapshots are stored per-camera at:
#   {VEHICLE_SNAPSHOT_DIR}/{camera_id}/{YYYY-MM-DD}/{HH-MM-SS}_{class}.jpg
# Legacy (pre-fan-out) snapshots may exist at:
#   {VEHICLE_SNAPSHOT_DIR}/{YYYY-MM-DD}/{HH-MM-SS}_{class}.jpg
# These helpers walk both layouts and consolidate.

def _is_camera_dir(name: str) -> bool:
    """A subdir of VEHICLE_SNAPSHOT_DIR is treated as a camera if it does NOT
    look like a YYYY-MM-DD date."""
    try:
        datetime.strptime(name, "%Y-%m-%d")
        return False
    except ValueError:
        return True


def _enumerate_day_dirs(camera: str = "") -> dict:
    """
    Return {date_str: [(camera_id_or_empty, dir_path), ...]} across all cameras.
    If `camera` is specified, scope to that camera only.
    Camera id is "" for legacy flat-layout entries.
    """
    base = ctx.VEHICLE_SNAPSHOT_DIR
    out: dict[str, list] = {}
    if not os.path.isdir(base):
        return out
    for name in os.listdir(base):
        path = os.path.join(base, name)
        if not os.path.isdir(path):
            continue
        if _is_camera_dir(name):
            # Per-camera subdir
            if camera and name != camera:
                continue
            try:
                for day_name in os.listdir(path):
                    day_path = os.path.join(path, day_name)
                    if os.path.isdir(day_path):
                        try:
                            datetime.strptime(day_name, "%Y-%m-%d")
                            out.setdefault(day_name, []).append((name, day_path))
                        except ValueError:
                            continue
            except Exception:
                continue
        else:
            # Legacy date directory at the root
            if camera:
                continue  # legacy entries can't be attributed to a camera
            try:
                datetime.strptime(name, "%Y-%m-%d")
                out.setdefault(name, []).append(("", path))
            except ValueError:
                continue
    return out


@router.get("/days")
async def list_days(camera: str = ""):
    """
    List available day folders containing vehicle snapshots.
    Returns [{date, count}] sorted newest-first.
    Optional `?camera=<id>` scopes to one camera.
    """
    try:
        day_map = _enumerate_day_dirs(camera=camera)
    except Exception:
        ctx.logger.exception("Browse days error")
        return JSONResponse(status_code=500, content={"error": "Failed to list days — see dashboard logs for details"})

    days = []
    for date_str in sorted(day_map.keys(), reverse=True):
        total = 0
        track_count = 0
        for _cam, day_path in day_map[date_str]:
            try:
                for entry in os.listdir(day_path):
                    full = os.path.join(day_path, entry)
                    if entry.lower().endswith(".jpg"):
                        total += 1
                    elif os.path.isdir(full) and entry.startswith("vehicle_"):
                        # Phase 1 per-track dir (hero.jpg + angle_NN.jpg + metadata.json)
                        # — surfaced as the "Vehicle crops taken (N)" modal trigger.
                        track_count += 1
            except Exception:
                continue
        days.append({
            "date": date_str,
            "count": total,
            "track_count": track_count,
        })
    return days


@router.get("/days/{date}")
async def list_day_snapshots(date: str, camera: str = ""):
    """
    List snapshot files for a specific day across cameras (or scoped to one).
    Returns [{filename, time, vehicle_class, camera, url}] sorted newest-first.
    """
    # Validate date format to prevent path traversal
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "Invalid date format, use YYYY-MM-DD"})

    day_map = _enumerate_day_dirs(camera=camera)
    snapshots = []
    try:
        for src_cam, day_path in day_map.get(date, []):
            for fname in sorted(os.listdir(day_path), reverse=True):
                if not fname.lower().endswith(".jpg"):
                    continue
                base_name = fname.rsplit(".", 1)[0]
                parts = base_name.split("_", 1)
                time_str = parts[0] if parts else ""
                vehicle_class = parts[1] if len(parts) > 1 else "vehicle"
                # URL encodes camera in path; legacy entries use empty segment
                cam_segment = src_cam if src_cam else "_legacy"
                snapshots.append({
                    "filename": fname,
                    "time": time_str.replace("-", ":"),
                    "vehicle_class": vehicle_class,
                    "camera": src_cam,
                    "url": f"/api/browse/snapshot/{cam_segment}/{date}/{fname}",
                })
    except Exception:
        ctx.logger.exception(f"Browse day {date} error")
        return JSONResponse(status_code=500, content={"error": "Failed to list snapshots — see dashboard logs for details"})

    # Re-sort by time desc across cameras
    snapshots.sort(key=lambda s: s["time"], reverse=True)
    return snapshots


@router.get("/tracks/{date}")
async def list_day_tracks(date: str, camera: str = ""):
    """List per-track snapshot groups for a day (Phase 1 vehicle-attributes
    layout). Each entry is one track_id directory with hero + angle thumbs.

    Returns [{
        track_id, camera, date, time, hero_url, angle_urls: [...],
        vehicle_class, event_kind, duration_seconds, voting_samples,
        attributes  // null fields in Phase 1, populated in Phase 3
    }] sorted newest-first.
    """
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return JSONResponse(status_code=400, content={"error": "invalid date"})

    base = ctx.VEHICLE_SNAPSHOT_DIR
    # Realpath base for containment checks below. Even though `cam` is
    # regex-stripped to alphanumerics+_- and `date` is regex-validated to
    # YYYY-MM-DD, CodeQL needs the explicit realpath+startswith pattern to
    # recognize the path as sanitized — same approach as
    # routes/events.py:resolve_event_snapshot_path and the per-track file
    # endpoint at line 253 below.
    base_real = os.path.realpath(base) + os.sep
    # Enumerate camera subdirs (same logic as _is_camera_dir)
    if camera:
        cams_to_scan = [camera]
    else:
        cams_to_scan = []
        if os.path.isdir(base):
            for name in os.listdir(base):
                if os.path.isdir(os.path.join(base, name)) and _is_camera_dir(name):
                    cams_to_scan.append(name)

    tracks = []
    for cam in cams_to_scan:
        cam_safe = re.sub(r"[^a-zA-Z0-9_-]", "", cam)
        day_dir = os.path.realpath(os.path.join(base, cam_safe, date))
        # Defense-in-depth: refuse anything that escaped the snapshot root
        # via a symlink or a `..` segment that snuck past the regex.
        if not day_dir.startswith(base_real):
            continue
        if not os.path.isdir(day_dir):
            continue
        for entry in os.scandir(day_dir):
            if not entry.is_dir():
                continue
            meta_path = os.path.join(entry.path, "metadata.json")
            if not os.path.isfile(meta_path):
                continue
            try:
                with open(meta_path) as fh:
                    meta = json.load(fh)
            except (OSError, ValueError):
                continue
            track_id = meta.get("track_id", entry.name)
            angles = sorted(
                f.name for f in os.scandir(entry.path)
                if f.is_file() and f.name.startswith("angle_")
                and f.name.endswith(".jpg")
            )
            tracks.append({
                "track_id": track_id,
                "camera": cam_safe,
                "date": date,
                "time": datetime.fromtimestamp(meta.get("first_seen", 0)).strftime("%H:%M:%S"),
                "first_seen": meta.get("first_seen"),
                "hero_url": f"/api/browse/tracks/{date}/{cam_safe}/{track_id}/hero.jpg",
                "angle_urls": [
                    f"/api/browse/tracks/{date}/{cam_safe}/{track_id}/{a}"
                    for a in angles
                ],
                "vehicle_class": meta.get("vehicle_class", "vehicle"),
                "event_kind": meta.get("event_kind", ""),
                "duration_seconds": meta.get("duration_seconds", 0),
                "voting_samples": meta.get("voting_samples", 1),
                "attributes": meta.get("attributes", {}),
            })

    tracks.sort(key=lambda t: t.get("first_seen") or 0, reverse=True)
    return tracks


@router.get("/tracks/{date}/{camera}/{track_id}/{filename}",
            name="serve_track_image")
async def serve_track_image(date: str, camera: str, track_id: str,
                            filename: str):
    """Serve hero.jpg or angle_NN.jpg from a per-track directory.

    Defense: all four path components match strict character classes AND
    the resolved real path must stay inside VEHICLE_SNAPSHOT_DIR (same
    containment check pattern as routes/events.py:get_event_snapshot).
    """
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return JSONResponse(status_code=400, content={"error": "invalid date"})
    if not re.match(r"^[a-zA-Z0-9_-]+$", camera):
        return JSONResponse(status_code=400, content={"error": "invalid camera"})
    if not re.match(r"^[a-zA-Z0-9_-]+$", track_id):
        return JSONResponse(status_code=400, content={"error": "invalid track_id"})
    if not re.match(r"^(hero\.jpg|angle_\d{2}\.jpg)$", filename):
        return JSONResponse(status_code=400, content={"error": "invalid filename"})

    candidate = os.path.realpath(os.path.join(ctx.VEHICLE_SNAPSHOT_DIR, camera,
                                              date, track_id, filename))
    root_real = os.path.realpath(ctx.VEHICLE_SNAPSHOT_DIR)
    if not candidate.startswith(root_real + os.sep):
        return JSONResponse(status_code=400, content={"error": "out of range"})
    if not os.path.isfile(candidate):
        return JSONResponse(status_code=404, content={"error": "not found"})
    return FileResponse(candidate, media_type="image/jpeg")


@router.get("/snapshot/{camera_or_legacy}/{date}/{filename}")
async def serve_snapshot(camera_or_legacy: str, date: str, filename: str):
    """
    Serve a vehicle snapshot from disk.
    `camera_or_legacy` is either a camera id or the literal "_legacy" for
    pre-fan-out snapshots stored at the flat root.
    """
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "Invalid date"})

    safe_name = os.path.basename(filename)
    safe_cam = os.path.basename(camera_or_legacy)
    if safe_cam == "_legacy":
        path = os.path.join(ctx.VEHICLE_SNAPSHOT_DIR, date, safe_name)
    else:
        path = os.path.join(ctx.VEHICLE_SNAPSHOT_DIR, safe_cam, date, safe_name)

    if not os.path.isfile(path):
        return JSONResponse(status_code=404, content={"error": "Snapshot not found"})

    with open(path, "rb") as f:
        data = f.read()

    return Response(content=data, media_type="image/jpeg")


# Backward-compat: old /api/browse/snapshot/{date}/{filename} (no camera segment)
# tries legacy root first, then walks all camera subdirs.
@router.get("/snapshot/{date}/{filename}", name="serve_snapshot_legacy")
async def serve_snapshot_legacy(date: str, filename: str):
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "Invalid date"})
    safe_name = os.path.basename(filename)

    # Try legacy flat first
    legacy_path = os.path.join(ctx.VEHICLE_SNAPSHOT_DIR, date, safe_name)
    if os.path.isfile(legacy_path):
        with open(legacy_path, "rb") as f:
            return Response(content=f.read(), media_type="image/jpeg")

    # Walk camera subdirs
    base = ctx.VEHICLE_SNAPSHOT_DIR
    if os.path.isdir(base):
        for name in os.listdir(base):
            if not _is_camera_dir(name):
                continue
            cam_path = os.path.join(base, name, date, safe_name)
            if os.path.isfile(cam_path):
                with open(cam_path, "rb") as f:
                    return Response(content=f.read(), media_type="image/jpeg")

    return JSONResponse(status_code=404, content={"error": "Snapshot not found"})


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
