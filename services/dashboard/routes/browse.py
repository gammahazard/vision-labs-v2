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
    GET  /api/browse/tracks/{date}   — Per-track HD-crop groups (Phase 1 vehicle-attributes)
    POST /api/browse/label/{date}/{camera}/{track_dir} — Save user labels for a track (Phase 4 labeling)
    GET  /api/browse/label-classes   — Class lists for the label-form dropdowns
"""

import json
import os
import re
import tempfile
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response
import httpx

import routes as ctx
from contracts.tz import TZ_LOCAL  # validated single source of truth

router = APIRouter(prefix="/api/browse", tags=["browse"])

# Where the vehicle-attributes class JSONs are bind-mounted in the
# dashboard container. See docker-compose.yml's dashboard.volumes block.
# Files: color_classes.json (10), body_classes.json (9), make_classes.json
# (49), model_classes.json (196), make_to_models.json (49-key dict).
_VA_CLASSES_DIR = "/app/vehicle_attributes_classes"


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
            # The URL path component MUST be the actual on-disk dir name
            # (entry.name) — that's what serve_track_image resolves. The
            # JSON `track_id` field shows the friendly vehicle_NNNN id
            # from metadata.json for UI display. Since storage.py now
            # appends the first-seen epoch to dir names to avoid
            # collisions across tracker restarts, entry.name and
            # meta["track_id"] are no longer the same string.
            dir_id = entry.name
            display_id = meta.get("track_id", entry.name)
            angles = sorted(
                f.name for f in os.scandir(entry.path)
                if f.is_file() and f.name.startswith("angle_")
                and f.name.endswith(".jpg")
            )
            tracks.append({
                "track_id": display_id,
                "dir_id": dir_id,
                "camera": cam_safe,
                "date": date,
                "time": datetime.fromtimestamp(meta.get("first_seen", 0), tz=TZ_LOCAL).strftime("%H:%M:%S"),
                "first_seen": meta.get("first_seen"),
                "hero_url": f"/api/browse/tracks/{date}/{cam_safe}/{dir_id}/hero.jpg",
                "angle_urls": [
                    f"/api/browse/tracks/{date}/{cam_safe}/{dir_id}/{a}"
                    for a in angles
                ],
                "vehicle_class": meta.get("vehicle_class", "vehicle"),
                "event_kind": meta.get("event_kind", ""),
                "duration_seconds": meta.get("duration_seconds", 0),
                "voting_samples": meta.get("voting_samples", 1),
                "attributes": meta.get("attributes", {}),
                # Phase 4 labeling: surfaced so the UI knows what's
                # already labeled vs needs labeling. Empty dict (not
                # null) when unset so the frontend doesn't have to
                # null-guard. metadata.json may not have this key at
                # all on tracks flushed before Phase 4 shipped.
                "user_labels": meta.get("user_labels", {}),
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


@router.delete("/tracks/{date}/{camera}/{track_id}/{filename}")
async def delete_track_image(date: str, camera: str, track_id: str,
                              filename: str):
    """Delete one crop (hero.jpg or angle_NN.jpg) from a track directory.

    Used when a particular crop is too blurry/occluded to be useful
    training data, but the rest of the track's crops + the user_labels
    are still good.

    Behavior:
      - If `filename` is an angle and other crops remain → just unlink it
      - If `filename` is hero.jpg AND at least one angle remains →
        unlink hero AND rename the lowest-numbered angle to hero.jpg
        so the track stays viewable (Browse expects hero.jpg to exist)
      - If this is the LAST remaining crop in the track → refuse with
        409 (deleting it would orphan metadata.json + user_labels). The
        UI is expected to grey out the ✕ button in this case; the server
        check is the defense.

    Returns: { ok: bool, removed: str, promoted: str | None, error: str | None }
    where `promoted` is the angle filename that was renamed to hero, if any.

    Same path-containment defense as serve_track_image — all four
    components regex-validated + realpath + startswith(root).
    """
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return JSONResponse(status_code=400, content={"ok": False, "error": "invalid date"})
    if not re.match(r"^[a-zA-Z0-9_-]+$", camera):
        return JSONResponse(status_code=400, content={"ok": False, "error": "invalid camera"})
    if not re.match(r"^[a-zA-Z0-9_-]+$", track_id):
        return JSONResponse(status_code=400, content={"ok": False, "error": "invalid track_id"})
    if not re.match(r"^(hero\.jpg|angle_\d{2}\.jpg)$", filename):
        return JSONResponse(status_code=400, content={"ok": False, "error": "invalid filename"})

    root_real = os.path.realpath(ctx.VEHICLE_SNAPSHOT_DIR)
    track_dir = os.path.realpath(os.path.join(
        ctx.VEHICLE_SNAPSHOT_DIR, camera, date, track_id))
    if not track_dir.startswith(root_real + os.sep):
        return JSONResponse(status_code=400, content={"ok": False, "error": "out of range"})
    if not os.path.isdir(track_dir):
        return JSONResponse(status_code=404, content={"ok": False, "error": "track not found"})

    target = os.path.join(track_dir, filename)
    if not os.path.isfile(target):
        return JSONResponse(status_code=404, content={"ok": False, "error": "crop not found"})

    # Enumerate remaining crops in the track (excluding the target).
    other_crops = sorted([
        f for f in os.listdir(track_dir)
        if f != filename
        and (f == "hero.jpg" or re.match(r"^angle_\d{2}\.jpg$", f))
    ])
    if not other_crops:
        return JSONResponse(
            status_code=409,
            content={"ok": False,
                     "error": "this is the last crop in the track — refused "
                              "(deleting it would orphan the track + labels). "
                              "Use the per-track skip option instead."},
        )

    promoted = None
    try:
        os.unlink(target)
        # If we removed hero.jpg, promote the lowest-numbered remaining
        # angle to hero. The Browse modal + tracks endpoint both assume
        # hero.jpg exists; without this rename, the next /tracks fetch
        # would return broken hero_url.
        if filename == "hero.jpg":
            angle_remaining = [f for f in other_crops if f.startswith("angle_")]
            if angle_remaining:
                promote_src = os.path.join(track_dir, angle_remaining[0])
                promote_dst = os.path.join(track_dir, "hero.jpg")
                os.rename(promote_src, promote_dst)
                promoted = angle_remaining[0]
                ctx.logger.info(
                    f"Browse remove-crop: deleted hero from {track_dir}, "
                    f"promoted {promoted} → hero.jpg"
                )
            else:
                ctx.logger.info(
                    f"Browse remove-crop: deleted hero from {track_dir} "
                    f"(no angles to promote — track will be unviewable until "
                    f"reflushed; this shouldn't happen because we counted "
                    f"other_crops > 0 above)"
                )
        else:
            ctx.logger.info(f"Browse remove-crop: deleted {filename} from {track_dir}")
    except OSError as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": f"couldn't remove crop: {e}"},
        )

    return {"ok": True, "removed": filename, "promoted": promoted, "error": None}


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


# ---------------------------------------------------------------------------
# Phase 4 labeling — user-supplied ground-truth labels for the multi-head
# classifier's predictions. Saved into metadata.json's `user_labels` block;
# never overwritten by the vehicle-attributes service (which only writes
# the `attributes` block on flush). Two endpoints:
#   GET  /api/browse/label-classes — dropdown contents for the UI form
#   POST /api/browse/label/{date}/{camera}/{track_dir} — save a label
# A future PR adds k-NN-based make/model prediction backed by these labels.
# ---------------------------------------------------------------------------

# In-memory cache of the class JSONs. Loaded on first request, refreshed
# whenever the file mtime changes (so a rebuild of vehicle-attributes
# that updates the class lists doesn't require a dashboard restart).
_class_cache = {"mtime": 0, "data": None}


def _load_label_classes() -> dict:
    """Return all class lists from /app/vehicle_attributes_classes/.

    Refreshes on mtime change. If the dir doesn't exist (older docker-
    compose.yml without the bind mount), returns empty lists — the UI
    falls back to free-text inputs for everything.
    """
    files = [
        "color_classes.json", "body_classes.json",
        "make_classes.json", "model_classes.json",
        "make_to_models.json",
    ]
    # Compute the max mtime across all files; if newer than cache, reload.
    try:
        latest = max(
            os.path.getmtime(os.path.join(_VA_CLASSES_DIR, f))
            for f in files
            if os.path.exists(os.path.join(_VA_CLASSES_DIR, f))
        )
    except ValueError:
        latest = 0  # dir empty or missing

    if _class_cache["data"] is not None and latest <= _class_cache["mtime"]:
        return _class_cache["data"]

    out = {
        "colors": [],
        "body_types": [],
        "makes": [],
        "models": [],
        "make_to_models": {},
    }
    name_map = {
        "color_classes.json": "colors",
        "body_classes.json": "body_types",
        "make_classes.json": "makes",
        "model_classes.json": "models",
        "make_to_models.json": "make_to_models",
    }
    for fname, key in name_map.items():
        path = os.path.join(_VA_CLASSES_DIR, fname)
        if not os.path.exists(path):
            continue
        try:
            with open(path) as fh:
                out[key] = json.load(fh)
        except (OSError, ValueError) as e:
            ctx.logger.warning(f"Failed to load {fname}: {e}")

    _class_cache["mtime"] = latest
    _class_cache["data"] = out
    return out


@router.get("/label-classes")
async def get_label_classes():
    """Class lists for the label-form dropdowns in the Browse → crops modal.

    Returns:
      {
        "colors":         ["yellow", "orange", ...],     # 10 entries
        "body_types":     ["convertible", "coupe", ...],  # 9 entries
        "makes":          ["AM General", "Acura", ...],   # 49 entries
        "models":         ["AM General Hummer SUV 2000", ...], # 196 entries
        "make_to_models": {"AM General": [...], "Acura": [...], ...}
      }
    """
    return _load_label_classes()


def _resolve_track_meta_path(date: str, camera: str, track_dir: str) -> str | None:
    """Resolve a track's metadata.json path with the same realpath +
    containment check used by serve_track_image. Returns None if any
    component is malformed or if the resolved path escapes the snapshot
    root (path-traversal defense)."""
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return None
    cam_safe = re.sub(r"[^a-zA-Z0-9_-]", "", camera)
    if cam_safe != camera or not cam_safe:
        return None
    # track_dir is the on-disk dir name like "vehicle_0050_1779561457".
    # Allow only the same characters that storage.py emits.
    if not re.match(r"^vehicle_[0-9]+_[0-9]+$", track_dir):
        return None

    base = ctx.VEHICLE_SNAPSHOT_DIR
    base_real = os.path.realpath(base) + os.sep
    candidate = os.path.realpath(
        os.path.join(base, cam_safe, date, track_dir, "metadata.json")
    )
    if not candidate.startswith(base_real):
        return None
    return candidate


@router.post("/label/{date}/{camera}/{track_dir}")
async def save_track_label(date: str, camera: str, track_dir: str,
                            request: Request):
    """Save user labels for a track. Body keys (all optional except either
    skipped=true OR at least one non-empty class label):

      {
        "color":      "blue",     # must be in colors class list
        "body_type":  "sedan",    # must be in body_types class list
        "make":       "Toyota",   # FREE TEXT — k-NN doesn't need a class
        "model":      "Sienna 2018",  # FREE TEXT
        "skipped":    false,
        "skip_reason": "occluded"  # optional
      }

    Writes to metadata.json's `user_labels` block atomically (tempfile
    + os.replace). Re-labeling overwrites the previous user_labels.

    Returns: { ok: bool, user_labels: {...}, error: str | None }
    """
    meta_path = _resolve_track_meta_path(date, camera, track_dir)
    if not meta_path:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "invalid date/camera/track_dir"},
        )
    if not os.path.exists(meta_path):
        return JSONResponse(
            status_code=404,
            content={"ok": False, "error": "track not found"},
        )

    try:
        body = await request.json()
    except (ValueError, json.JSONDecodeError):
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "invalid JSON body"},
        )

    skipped = bool(body.get("skipped", False))
    color = (body.get("color") or "").strip()
    body_type = (body.get("body_type") or "").strip()
    make = (body.get("make") or "").strip()
    model = (body.get("model") or "").strip()

    # Must have at least ONE field set, OR be marked skipped. An
    # otherwise-empty label is rejected — empty saves would just churn
    # the metadata.json mtime without recording anything.
    if not skipped and not (color or body_type or make or model):
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "label is empty (set at least one field or pass skipped=true)"},
        )

    # Validate color/body against the class lists (these heads have
    # fixed class sets in the multihead model; labels outside the set
    # can't be used by the retrain script even if we accept them).
    # Make/model are intentionally free text — k-NN doesn't care about
    # the class set, and the UI will autocomplete known values.
    classes = _load_label_classes()
    if color and classes.get("colors") and color not in classes["colors"]:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": f"unknown color '{color}'; must be one of {classes['colors']}"},
        )
    if body_type and classes.get("body_types") and body_type not in classes["body_types"]:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": f"unknown body_type '{body_type}'; must be one of {classes['body_types']}"},
        )

    # Load existing metadata.json, merge user_labels, write atomically.
    try:
        with open(meta_path) as fh:
            meta = json.load(fh)
    except (OSError, ValueError) as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": f"couldn't read metadata.json: {e}"},
        )

    # When skipped=true, ignore any color/body/make/model the caller
    # included. Saving "skipped + color=blue" produces contradictory
    # state that downstream code (k-NN, retrain) has to disambiguate.
    # Cleaner to enforce here: skipped tracks have NO label fields.
    user_labels = {
        "color": None if skipped else (color or None),
        "body_type": None if skipped else (body_type or None),
        "make": None if skipped else (make or None),
        "model": None if skipped else (model or None),
        "skipped": skipped,
        "skip_reason": body.get("skip_reason") if skipped else None,
        "labeled_at": datetime.now(tz=TZ_LOCAL).isoformat(timespec="seconds"),
        # Future: read from session for multi-user deployments. For now,
        # single-user home setup → "admin" is the only labeler.
        "labeler": "admin",
    }
    meta["user_labels"] = user_labels

    # Atomic write: tempfile in the same dir + os.replace. Same pattern
    # as helpers/env_writer.py — survives a mid-write crash and avoids
    # the partial-file truncation case that a bare open(meta_path, 'w')
    # would expose.
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=".metadata.json.tmp.",
            dir=os.path.dirname(meta_path),
        )
        try:
            with os.fdopen(tmp_fd, "w") as fh:
                json.dump(meta, fh, indent=2)
            os.replace(tmp_path, meta_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except OSError as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": f"couldn't write metadata.json: {e}"},
        )

    return {"ok": True, "user_labels": user_labels, "error": None}
