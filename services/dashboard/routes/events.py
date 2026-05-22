"""
routes/events.py — Event feed endpoint.

PURPOSE:
    GET /api/events — Read recent events from the Redis event stream.
    GET /api/events/{event_id}/snapshot — Serve saved camera snapshot JPEG.
    GET /api/events/{event_id}/analysis — Get AI scene analysis for an event.
    Used by events.js in the frontend.
"""

import os
import json
import re
from datetime import datetime, timedelta

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response
import redis

import routes as ctx

router = APIRouter(prefix="/api", tags=["events"])

SNAPSHOT_DIR = os.environ.get("SNAPSHOT_DIR", "/data/snapshots")
EVENT_JOURNAL_DIR = os.environ.get("EVENT_JOURNAL_DIR", "/data/events")
from contracts.tz import TZ_LOCAL as _TZ_LOCAL  # validated single source of truth


def _enabled_camera_ids() -> list:
    """Snapshot enabled cameras from the registry. Falls back to primary.
    Thin wrapper over cameras.enabled_camera_ids() so all callers share
    the same registry-read logic."""
    import cameras as _camreg
    ids = _camreg.enabled_camera_ids()
    return ids if ids else [ctx.CAMERA_ID]


def _ms_from_stream_id(mid) -> int:
    """Extract the millisecond timestamp from a Redis stream id 'MS-SEQ'."""
    try:
        return int(str(mid).split("-")[0])
    except Exception:
        return 0


def _read_journal(cam_ids: list, cutoff_ms: int | None,
                  count: int, seen_ids: set) -> list[tuple]:
    """Read events from the on-disk JSONL journal, newest-first.

    Returns a list of (event_id, data_dict, camera_id) tuples, matching the
    shape used by the Redis stream loop so the caller can render them uniformly.

    - `cutoff_ms`: if set, only entries with timestamp_ms < cutoff are returned.
    - `seen_ids`: ids already returned from Redis — skipped to avoid duplicates
      when the most recent journal day overlaps with the in-memory stream.
    """
    if not os.path.isdir(EVENT_JOURNAL_DIR):
        return []

    cam_set = set(cam_ids) if cam_ids else None
    cutoff_ts = (cutoff_ms / 1000.0) if cutoff_ms is not None else None

    # Start scanning from the day of the cutoff (or today if no cutoff).
    if cutoff_ms is not None:
        start_date = datetime.fromtimestamp(cutoff_ms / 1000.0, tz=_TZ_LOCAL).date()
    else:
        start_date = datetime.now(_TZ_LOCAL).date()

    out: list[tuple] = []
    current = start_date
    # Bound the search to a year of history so we don't scan forever
    # if the cutoff predates the oldest journal file.
    for _ in range(366):
        path = os.path.join(EVENT_JOURNAL_DIR, f"{current.isoformat()}.jsonl")
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    lines = f.readlines()
            except OSError:
                lines = []

            day: list[tuple[float, dict]] = []
            for line in lines:
                try:
                    entry = json.loads(line)
                except (ValueError, json.JSONDecodeError):
                    continue
                eid = entry.get("id", "")
                if eid in seen_ids:
                    continue
                try:
                    ts = float(entry.get("timestamp", 0))
                except (ValueError, TypeError):
                    continue
                if cutoff_ts is not None and ts >= cutoff_ts:
                    continue
                if cam_set and entry.get("camera") not in cam_set:
                    continue
                day.append((ts, entry))

            # Sort newest-first within the day (file is append-only chronological)
            day.sort(key=lambda x: x[0], reverse=True)
            for _ts, entry in day:
                out.append((entry.get("id", ""), entry, entry.get("camera", "")))
                if len(out) >= count:
                    return out

        current -= timedelta(days=1)

    return out


@router.get("/events")
async def get_events(count: int = 50, camera: str = "", before: str = ""):
    """
    Return events from one or more camera streams.

    - camera="" or "all"  → merge events across every enabled camera
    - camera="<id>"       → only that camera
    - before=<event_id>   → cursor: return events strictly OLDER than this id
                            (powers the home page's "Load older" pagination)

    Pulls from the Redis stream first. If the stream doesn't have enough
    history (capped at maxlen=1000 per camera), falls through to the
    JSONL journal on disk for older events. Response includes `has_more`
    so the frontend can hide the "Load older" button when we've reached
    the end.
    """
    from contracts.streams import EVENT_STREAM as _EVT_TMPL, stream_key as _stream_key
    from event_renderer import render_event

    try:
        cam = (camera or "").strip().lower()
        if cam in ("", "all"):
            cam_ids = _enabled_camera_ids()
        else:
            cam_ids = [camera]

        # ---- Phase 1: pull from Redis stream(s) ----
        # `(ID` is the Redis exclusive-bound syntax — gives items with id < ID.
        max_id = f"({before}" if before else "+"
        merged: list[tuple] = []
        for cid in cam_ids:
            evt_stream = _stream_key(_EVT_TMPL, camera_id=cid)
            try:
                events_raw = ctx.r.xrevrange(evt_stream, max=max_id, count=count)
            except redis.ResponseError:
                # Bad cursor (or empty stream) — skip this camera silently
                events_raw = []
            for event_id, data in events_raw:
                merged.append((event_id, dict(data), cid))

        merged.sort(key=lambda x: _ms_from_stream_id(x[0]), reverse=True)
        merged = merged[:count]

        # ---- Phase 2: fall through to journal if Redis didn't satisfy ----
        if len(merged) < count:
            # Cutoff for the journal scan: strictly older than the oldest Redis
            # result, or older than the request cursor if Redis gave us nothing.
            if merged:
                cutoff_ms = _ms_from_stream_id(merged[-1][0])
            elif before:
                cutoff_ms = _ms_from_stream_id(before)
            else:
                cutoff_ms = None  # no cursor — let journal return newest

            remaining = count - len(merged)
            seen_ids = {mid for mid, _d, _c in merged}
            journal_results = _read_journal(cam_ids, cutoff_ms, remaining, seen_ids)
            merged.extend(journal_results)

        # ---- Phase 3: render & return ----
        # Filter out internal-only event types that consumers downstream of
        # the events stream use (vehicle-attributes service), not the
        # user-facing events panel:
        #   - vehicle_sample: tracker emits at SAMPLE_INTERVAL_FRAMES cadence
        #     so the attribute service can crop HD frames. No user signal.
        #   - vehicle_gone: ghost-buffer expiry, always paired with either a
        #     prior vehicle_idle (user already notified) or a drive-by track
        #     end (nothing actionable). vehicle_left still surfaces for the
        #     idle-leave case via the idle_alerted gate in the tracker.
        _INTERNAL_EVENT_TYPES = ("vehicle_sample", "vehicle_gone")
        events = []
        for event_id, data, src_cam in merged:
            if data.get("event_type") in _INTERNAL_EVENT_TYPES:
                continue
            evt = {
                "id": event_id,
                "event_type": data.get("event_type", ""),
                "person_id": data.get("person_id", ""),
                "timestamp": data.get("timestamp", "0"),
                "duration": data.get("duration", "0"),
                "direction": data.get("direction", ""),
                "action": data.get("action", "unknown"),
                # Prefer data.camera_id (set by fanned-out poller) but fall back
                # to the source stream id so old events still have a camera tag
                "camera_id": data.get("camera_id", src_cam),
                "zone": data.get("zone", ""),
                "alert_level": data.get("alert_level", ""),
                "alert_triggered": data.get("alert_triggered", "false"),
                "prev_action": data.get("prev_action", ""),
                "identity_name": data.get("identity_name", ""),
                "vehicle_class": data.get("vehicle_class", ""),
                "vehicle_confidence": data.get("vehicle_confidence", ""),
                "snapshot_key": data.get("snapshot_key", ""),
                # face_reconciled-only details — populated when an enrollment,
                # label, scan, or startup reconcile absorbed gallery rows.
                # Both are JSON-encoded arrays (strings) for the frontend to parse.
                "promoted_face_ids": data.get("promoted_face_ids", ""),
                "similarities": data.get("similarities", ""),
                "count": data.get("count", ""),
                # Telegram-specific (only set for unauthorized_access events)
                "telegram_username": data.get("telegram_username", ""),
                "telegram_user_id": data.get("telegram_user_id", ""),
            }
            ai_desc = ctx.r.get(f"scene_analysis:{event_id}")
            if ai_desc:
                evt["ai_description"] = ai_desc
            # Single source of truth for display: see services/dashboard/event_renderer.py
            evt["render"] = render_event(evt)
            events.append(evt)

        # has_more is best-effort: if we filled the page, there's likely more.
        # If we returned fewer than requested, we've hit the end of storage.
        return {
            "events": events,
            "cameras": cam_ids,
            "has_more": len(events) >= count,
        }
    except redis.ConnectionError:
        return JSONResponse(status_code=503, content={"error": "Redis unavailable"})


def resolve_event_snapshot_path(event_id: str, camera_id: str = "") -> str | None:
    """Locate the on-disk snapshot for an event_id.

    Path search order:
      1. {SNAPSHOT_DIR}/{camera_id}/{event_id}.jpg (if camera_id provided)
      2. {SNAPSHOT_DIR}/{cam}/{event_id}.jpg for each enabled camera (fallback)
      3. {SNAPSHOT_DIR}/{event_id}.jpg (legacy flat layout, pre-fan-out)

    Returns the first path that exists, or None.

    The `event_id` comes from a URL path parameter, so we MUST refuse
    anything that isn't a real Redis stream id (`<unix-ms>-<seq>`) — a
    crafted id like `../../etc/foo` would otherwise let the caller read
    any `*.jpg` on the dashboard container's filesystem.
    """
    # Redis stream ids are always `<digits>-<digits>`. Old/manual events
    # in the journal may use `<digits>.<digits>` — accept both shapes.
    if not re.fullmatch(r"\d+[-.]\d+", event_id or ""):
        return None
    safe_id = event_id.replace(":", "-")
    # Also defence-in-depth: even after the regex check, ensure no path
    # separators slipped through (e.g. via decoded URL chars).
    if "/" in safe_id or "\\" in safe_id or ".." in safe_id:
        return None

    # Defense-in-depth helper: build a path under SNAPSHOT_DIR, then realpath
    # it and verify it actually stays inside SNAPSHOT_DIR. Catches both:
    #   1. Path-traversal in camera_id / event_id (e.g. ?camera=../../etc)
    #   2. Symlinks pointing outside the snapshot tree (someone could plant
    #      a symlink at /data/snapshots/cam1/foo.jpg → /etc/passwd)
    # CodeQL recognizes os.path.realpath + startswith-base as the canonical
    # sanitizer for path-injection, which closes the open #42-#45 alerts.
    snap_root = os.path.realpath(SNAPSHOT_DIR) + os.sep

    def _safe_path(*parts: str) -> str | None:
        candidate = os.path.realpath(os.path.join(SNAPSHOT_DIR, *parts))
        if not candidate.startswith(snap_root) and candidate != snap_root[:-1]:
            return None
        return candidate

    if camera_id:
        p = _safe_path(camera_id, f"{safe_id}.jpg")
        if p and os.path.exists(p):
            return p

    # Walk camera subdirs from registry
    try:
        raw = ctx.r.hgetall("cameras:registry") or {}
        for _cid, val in raw.items():
            try:
                entry = json.loads(val)
                cid = entry.get("id") or _cid
                p = _safe_path(cid, f"{safe_id}.jpg")
                if p and os.path.exists(p):
                    return p
            except Exception:
                continue
    except Exception:
        pass

    # Legacy flat path
    legacy = _safe_path(f"{safe_id}.jpg")
    if legacy and os.path.exists(legacy):
        return legacy
    return None


@router.get("/events/{event_id}/snapshot")
async def get_event_snapshot(event_id: str, camera: str = ""):
    """
    Serve the saved camera snapshot for a given event.
    Snapshots are stored per-camera at {SNAPSHOT_DIR}/{camera_id}/{event_id}.jpg.
    Pass `?camera=<id>` to skip the cross-camera search.
    """
    path = resolve_event_snapshot_path(event_id, camera_id=camera)
    if not path:
        return JSONResponse(status_code=404, content={"error": "Snapshot not found"})

    # resolve_event_snapshot_path already realpath-validates that path lives
    # under SNAPSHOT_DIR — re-check here at the actual open() sink so CodeQL's
    # local taint analysis sees the sanitizer adjacent to the open() call.
    snap_root = os.path.realpath(SNAPSHOT_DIR) + os.sep
    if not os.path.realpath(path).startswith(snap_root):
        return JSONResponse(status_code=404, content={"error": "Snapshot not found"})

    with open(path, "rb") as f:
        data = f.read()

    return Response(content=data, media_type="image/jpeg")


@router.get("/events/{event_id}/analysis")
async def get_event_analysis(event_id: str):
    """
    Return the AI scene analysis for a given event.
    The analysis is generated by the MiniCPM-V vision model when a
    notification is sent, and stored in Redis with a 24-hour TTL.
    """
    try:
        desc = ctx.r.get(f"scene_analysis:{event_id}")
        if desc:
            return {"event_id": event_id, "description": desc}
        return JSONResponse(status_code=404,
                            content={"error": "No analysis available"})
    except redis.ConnectionError:
        return JSONResponse(status_code=503,
                            content={"error": "Redis unavailable"})


@router.get("/vehicles/snapshot/{key:path}")
async def get_vehicle_snapshot(key: str):
    """
    Serve a vehicle snapshot stored in Redis by the tracker.

    Vehicle snapshots are stored with a 24h TTL in Redis keys like:
    vehicle_snapshot:{camera_id}:{timestamp}

    Also draws the bounding box if a companion bbox key exists.
    """
    try:
        # Use the shared binary Redis client (ctx.r has decode_responses=True
        # which would corrupt binary JPEG data)
        r_bin = ctx.r_bin
        data = r_bin.get(key)
        if not data:
            return JSONResponse(status_code=404, content={"error": "Snapshot expired or not found"})

        # Draw bbox if companion key exists
        bbox_raw = r_bin.get(f"{key}:bbox")
        if bbox_raw:
            try:
                import json, cv2, numpy as np
                bbox = json.loads(bbox_raw)
                if len(bbox) == 4:
                    np_arr = np.frombuffer(data, np.uint8)
                    img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
                    if img is not None:
                        x1, y1, x2, y2 = [int(v) for v in bbox]
                        cv2.rectangle(img, (x1, y1), (x2, y2), (0, 165, 255), 3)
                        _, data = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
                        data = data.tobytes()
            except Exception:
                pass  # Serve raw frame if drawing fails

        return Response(content=data, media_type="image/jpeg")
    except redis.ConnectionError:
        return JSONResponse(status_code=503, content={"error": "Redis unavailable"})

