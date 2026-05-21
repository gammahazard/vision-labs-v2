"""
routes/metrics.py — Prometheus metrics endpoint for Vision Labs.

PURPOSE:
    Exposes a /metrics endpoint in standard Prometheus text format.
    Collects application-level metrics by polling Redis streams and keys
    every 10 seconds. This lets Prometheus scrape pipeline health, GPU
    pause state, event counts, and feedback stats without modifying any
    of the detector/tracker containers.

RELATIONSHIPS:
    - Mounted by server.py as a FastAPI router
    - Reads from Redis via routes.r / routes.r_bin (shared state)
    - Scraped by Prometheus (see services/prometheus/prometheus.yml)
    - Feeds Grafana dashboards (see services/grafana/dashboards/vision-labs.json)
"""

import asyncio
import time
import json
import logging
import sys
import os

from fastapi import APIRouter, Response

from prometheus_client import (
    Gauge,
    Counter,
    generate_latest,
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    REGISTRY,
)

import routes as ctx

# Stream-key templates so we can build per-camera keys for every registered
# camera (rather than only the dashboard's primary). Templates live in the
# bind-mounted contracts/ directory.
sys.path.insert(0, "/app/contracts")
try:
    from streams import (
        FRAME_STREAM as _FRAME_TMPL,
        DETECTION_STREAM as _DET_TMPL,
        EVENT_STREAM as _EVT_TMPL,
        VEHICLE_STREAM as _VEH_TMPL,
        STATE_KEY as _STATE_TMPL,
        stream_key,
    )
except ImportError:  # pragma: no cover — only hits during test import
    _FRAME_TMPL = _DET_TMPL = _EVT_TMPL = _VEH_TMPL = _STATE_TMPL = ""
    def stream_key(t, **kw): return t.format(**kw)

logger = logging.getLogger("dashboard.metrics")

# ---------------------------------------------------------------------------
# Prometheus Metrics Definitions — every per-camera metric carries a
# `camera` label so Grafana can split / stack / filter by camera. Global
# metrics (gpu pause flag) stay label-less since they aren't camera-scoped.
# ---------------------------------------------------------------------------

# Pipeline (per camera)
vl_detections_total = Counter(
    "vl_detections_total",
    "Total person detections processed",
    ["camera"],
)
vl_vehicle_detections_total = Counter(
    "vl_vehicle_detections_total",
    "Total vehicle detections processed",
    ["camera"],
)
vl_events_total = Counter(
    "vl_events_total",
    "Total events by type",
    ["camera", "event_type"],
)
vl_active_persons = Gauge(
    "vl_active_persons",
    "Currently tracked people in camera view",
    ["camera"],
)
vl_inference_ms = Gauge(
    "vl_inference_ms",
    "Latest YOLO inference time in milliseconds",
    ["camera"],
)
vl_frames_per_second = Gauge(
    "vl_frames_per_second",
    "Current camera frame processing rate",
    ["camera"],
)
vl_stream_length = Gauge(
    "vl_stream_length",
    "Current Redis stream length",
    ["camera", "stream"],
)

# Notifications (per camera) — labeled by camera AND event type so a Grafana
# panel can chart "vehicle alerts per minute per camera".
vl_notifications_total = Counter(
    "vl_notifications_total",
    "Total Telegram notifications sent",
    ["camera", "type"],
)



# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
router = APIRouter()

# Per-camera last-seen IDs (was: single global strings — broke when more
# than one camera was active because counters from different cameras
# overwrote each other's cursor).
_last_det_id_by_cam: dict[str, str] = {}
_last_veh_id_by_cam: dict[str, str] = {}
_last_event_id_by_cam: dict[str, str] = {}
_collector_started: bool = False


def _enabled_camera_ids() -> list[str]:
    """Read the camera registry and return enabled camera ids, sorted.

    Falls back to the dashboard's primary CAMERA_ID env var if the
    registry is unreachable / empty so metrics keep working during
    first-boot before any cameras have been registered.
    """
    try:
        raw = ctx.r.hgetall("cameras:registry") if ctx.r else None
        if not raw:
            return [ctx.CAMERA_ID]
        out = []
        for cid, val in raw.items():
            try:
                entry = json.loads(val)
            except (ValueError, json.JSONDecodeError):
                continue
            if entry.get("enabled", True):
                out.append(entry.get("id", cid))
        return sorted(out) if out else [ctx.CAMERA_ID]
    except Exception:
        return [ctx.CAMERA_ID]


# ---------------------------------------------------------------------------
# /metrics endpoint
# ---------------------------------------------------------------------------
@router.get("/metrics")
async def prometheus_metrics():
    """Return all metrics in Prometheus text exposition format."""
    body = generate_latest(REGISTRY)
    return Response(content=body, media_type=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------------------
# /api/monitoring/health — lightweight JSON for the health summary cards
# ---------------------------------------------------------------------------
@router.get("/api/monitoring/health")
async def monitoring_health():
    """Quick health snapshot for the monitoring page summary cards."""
    try:
        r = ctx.r

        # Active persons from state hash (tracker writes via HSET with num_people + people JSON).
        # Was: r.get() on a hash + state.get("persons") — both wrong, silently returned 0.
        active = 0
        try:
            num_people_raw = r.hget(ctx.STATE_KEY, "num_people")
            if num_people_raw is not None:
                active = int(num_people_raw)
        except Exception:
            pass

        # Latest inference time
        inference = 0.0
        try:
            det_key = ctx.DETECTION_STREAM
            entries = r.xrevrange(det_key, count=1)
            if entries:
                inference = float(entries[0][1].get("inference_ms", "0"))
        except Exception:
            pass

        # Redis memory
        redis_mem_mb = 0
        try:
            info = r.info("memory")
            redis_mem_mb = round(info.get("used_memory", 0) / (1024 * 1024), 1)
        except Exception:
            pass

        # Event stream length (today's activity proxy)
        events_len = 0
        try:
            events_len = r.xlen(ctx.EVENT_STREAM)
        except Exception:
            pass

        return {
            "active_persons": active,
            "inference_ms": round(inference, 1),
            "redis_memory_mb": redis_mem_mb,
            "total_events": events_len,
        }
    except Exception as e:
        logger.exception("Health endpoint error")
        return {"error": "Health check failed — see dashboard logs for details"}


# ---------------------------------------------------------------------------
# Helper: count new entries via XRANGE from last-seen ID
# ---------------------------------------------------------------------------
def _count_new_entries(r, stream_key: str, last_id: str,
                       max_count: int = 1000) -> tuple:
    """
    Read new entries from a Redis stream since last_id.
    Returns (count_of_new_entries, new_last_id).
    Works correctly even when streams use MAXLEN trimming.
    """
    try:
        entries = r.xrange(stream_key, min=last_id, count=max_count)
        count = 0
        new_last_id = last_id
        for eid, _ in entries:
            if eid == last_id:
                continue  # Skip the entry we already counted
            count += 1
            new_last_id = eid
        return count, new_last_id
    except Exception:
        return 0, last_id


# ---------------------------------------------------------------------------
# Background Metrics Collector
# ---------------------------------------------------------------------------
async def start_metrics_collector():
    """
    Background task that polls Redis every 10 seconds and updates
    Prometheus gauges/counters. Runs as an asyncio task started from
    server.py's startup hook.
    """
    global _collector_started
    if _collector_started:
        return
    _collector_started = True

    logger.info("Metrics collector started — polling every 10s (per-camera labels)")

    # Wait for Redis to be ready
    await asyncio.sleep(3)

    # Seed last-seen IDs to current stream tip for every enabled camera.
    # Without this we'd retroactively count all historical entries in the
    # stream when this service starts up.
    try:
        r = ctx.r
        if r:
            for cam_id in _enabled_camera_ids():
                streams = {
                    "det": stream_key(_DET_TMPL, detector_type="pose", camera_id=cam_id),
                    "veh": stream_key(_VEH_TMPL, camera_id=cam_id),
                    "evt": stream_key(_EVT_TMPL, camera_id=cam_id),
                }
                for key, sname in streams.items():
                    tip = r.xrevrange(sname, count=1)
                    if not tip:
                        continue
                    if key == "det":
                        _last_det_id_by_cam[cam_id] = tip[0][0]
                    elif key == "veh":
                        _last_veh_id_by_cam[cam_id] = tip[0][0]
                    elif key == "evt":
                        _last_event_id_by_cam[cam_id] = tip[0][0]
            logger.info(f"Metrics collector seeded stream positions for "
                        f"{len(_enabled_camera_ids())} camera(s)")
    except Exception as e:
        logger.debug(f"Failed to seed stream positions: {e}")

    while True:
        try:
            r = ctx.r
            if r is None:
                await asyncio.sleep(10)
                continue

            cam_ids = _enabled_camera_ids()

            for cam_id in cam_ids:
                # Per-camera stream keys
                frame_s = stream_key(_FRAME_TMPL, camera_id=cam_id)
                det_s = stream_key(_DET_TMPL, detector_type="pose", camera_id=cam_id)
                veh_s = stream_key(_VEH_TMPL, camera_id=cam_id)
                evt_s = stream_key(_EVT_TMPL, camera_id=cam_id)
                state_k = stream_key(_STATE_TMPL, camera_id=cam_id)

                # First time we see this camera in the loop, seed all its
                # cursors to the CURRENT stream tip so we don't retroactively
                # count the entire history. Without this, adding a camera
                # at runtime double-counts every event already on the stream.
                if cam_id not in _last_det_id_by_cam:
                    try:
                        for stream, dct in (
                            (det_s, _last_det_id_by_cam),
                            (veh_s, _last_veh_id_by_cam),
                            (evt_s, _last_event_id_by_cam),
                        ):
                            tip = r.xrevrange(stream, count=1)
                            dct[cam_id] = tip[0][0] if tip else "0-0"
                        logger.info(
                            f"Metrics collector: seeded cursors for new camera {cam_id}"
                        )
                    except Exception as e:
                        logger.debug(f"Failed to seed cursors for {cam_id}: {e}")

                # ------ Stream lengths ------
                try:
                    vl_stream_length.labels(camera=cam_id, stream="frames").set(r.xlen(frame_s))
                    vl_stream_length.labels(camera=cam_id, stream="detections").set(r.xlen(det_s))
                    vl_stream_length.labels(camera=cam_id, stream="vehicles").set(r.xlen(veh_s))
                    vl_stream_length.labels(camera=cam_id, stream="events").set(r.xlen(evt_s))
                except Exception as e:
                    logger.debug(f"Stream length poll error for {cam_id}: {e}")

                # ------ New person detections ------
                try:
                    last = _last_det_id_by_cam.get(cam_id, "0-0")
                    count, new_last = _count_new_entries(r, det_s, last)
                    _last_det_id_by_cam[cam_id] = new_last
                    if count > 0:
                        vl_detections_total.labels(camera=cam_id).inc(count)
                except Exception:
                    pass

                # ------ New vehicle detections ------
                try:
                    last = _last_veh_id_by_cam.get(cam_id, "0-0")
                    count, new_last = _count_new_entries(r, veh_s, last)
                    _last_veh_id_by_cam[cam_id] = new_last
                    if count > 0:
                        vl_vehicle_detections_total.labels(camera=cam_id).inc(count)
                except Exception:
                    pass

                # ------ Frames/sec — from stream ts span ------
                try:
                    first = r.xrange(frame_s, count=1)
                    last_e = r.xrevrange(frame_s, count=1)
                    if first and last_e:
                        f_len = r.xlen(frame_s)
                        first_ts = int(first[0][0].split("-")[0]) / 1000.0
                        last_ts = int(last_e[0][0].split("-")[0]) / 1000.0
                        span = last_ts - first_ts
                        if span > 0:
                            vl_frames_per_second.labels(camera=cam_id).set(round(f_len / span, 2))
                except Exception:
                    pass

                # ------ Latest inference time ------
                try:
                    entries = r.xrevrange(det_s, count=1)
                    if entries:
                        ms = float(entries[0][1].get("inference_ms", "0"))
                        vl_inference_ms.labels(camera=cam_id).set(ms)
                except Exception:
                    pass

                # ------ Active persons (per camera) ------
                try:
                    num_people_raw = r.hget(state_k, "num_people")
                    val = int(num_people_raw) if num_people_raw is not None else 0
                    vl_active_persons.labels(camera=cam_id).set(val)
                except Exception:
                    pass

                # ------ Event counting (by type, per camera) ------
                try:
                    last = _last_event_id_by_cam.get(cam_id, "0-0")
                    new_events = r.xrange(evt_s, min=last, count=500)
                    for eid, data in new_events:
                        if eid == last:
                            continue
                        event_type = data.get("event_type", "unknown")
                        vl_events_total.labels(camera=cam_id, event_type=event_type).inc()
                        _last_event_id_by_cam[cam_id] = eid
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"Metrics collector error: {e}")

        await asyncio.sleep(10)
