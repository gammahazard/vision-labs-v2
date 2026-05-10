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

logger = logging.getLogger("dashboard.metrics")

# ---------------------------------------------------------------------------
# Prometheus Metrics Definitions
# ---------------------------------------------------------------------------

# Pipeline
vl_detections_total = Counter(
    "vl_detections_total",
    "Total person detections processed",
)
vl_vehicle_detections_total = Counter(
    "vl_vehicle_detections_total",
    "Total vehicle detections processed",
)
vl_events_total = Counter(
    "vl_events_total",
    "Total events by type",
    ["event_type"],
)
vl_active_persons = Gauge(
    "vl_active_persons",
    "Currently tracked people in camera view",
)
vl_inference_ms = Gauge(
    "vl_inference_ms",
    "Latest YOLO inference time in milliseconds",
)
vl_frames_per_second = Gauge(
    "vl_frames_per_second",
    "Current camera frame processing rate",
)
vl_stream_length = Gauge(
    "vl_stream_length",
    "Current Redis stream length",
    ["stream"],
)

# GPU
vl_gpu_pause_active = Gauge(
    "vl_gpu_pause_active",
    "Whether GPU generation is active (1=paused, 0=running)",
)

# Notifications
vl_notifications_total = Counter(
    "vl_notifications_total",
    "Total Telegram notifications sent",
    ["type"],
)

# Feedback



# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
router = APIRouter()

_last_det_id: str = "0-0"          # Last detection stream ID we've seen
_last_veh_id: str = "0-0"          # Last vehicle detection stream ID

_last_event_id: str = "0-0"        # Last event stream ID we've seen
_collector_started: bool = False


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

        # Active persons from state key
        active = 0
        try:
            state_raw = r.get(ctx.STATE_KEY)
            if state_raw:
                state = json.loads(state_raw)
                active = len(state.get("persons", []))
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

        # GPU pause status
        gpu_paused = bool(r.exists("gpu:generation_active"))

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
            "gpu_paused": gpu_paused,
            "redis_memory_mb": redis_mem_mb,
            "total_events": events_len,
        }
    except Exception as e:
        logger.error(f"Health endpoint error: {e}")
        return {"error": str(e)}


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
    global _last_det_id, _last_veh_id
    global _last_event_id, _collector_started

    if _collector_started:
        return
    _collector_started = True

    logger.info("Metrics collector started — polling every 10s")

    # Wait for Redis to be ready
    await asyncio.sleep(3)

    # Seed last-seen IDs to current stream tip (don't count old history)
    try:
        r = ctx.r
        if r:
            for stream, attr in [
                (ctx.DETECTION_STREAM, "_last_det_id"),
                (ctx.EVENT_STREAM, "_last_event_id"),
                (f"detections:vehicle:{ctx.CAMERA_ID}", "_last_veh_id"),
            ]:
                tip = r.xrevrange(stream, count=1)
                if tip:
                    globals()[attr] = tip[0][0]
            logger.info("Metrics collector seeded stream positions")

    except Exception as e:
        logger.debug(f"Failed to seed stream positions: {e}")

    while True:
        try:
            r = ctx.r
            if r is None:
                await asyncio.sleep(10)
                continue

            # ------ Stream lengths (gauges) ------
            try:
                det_len = r.xlen(ctx.DETECTION_STREAM)
                frame_len = r.xlen(ctx.FRAME_STREAM)
                event_len = r.xlen(ctx.EVENT_STREAM)
                veh_stream = f"detections:vehicle:{ctx.CAMERA_ID}"
                veh_len = r.xlen(veh_stream)

                vl_stream_length.labels(stream="detections").set(det_len)
                vl_stream_length.labels(stream="frames").set(frame_len)
                vl_stream_length.labels(stream="events").set(event_len)
                vl_stream_length.labels(stream="vehicles").set(veh_len)
            except Exception as e:
                logger.debug(f"Stream length poll error: {e}")

            # ------ Count new entries via XRANGE (counters) ------
            # Detections
            try:
                count, _last_det_id = _count_new_entries(
                    r, ctx.DETECTION_STREAM, _last_det_id)
                if count > 0:
                    vl_detections_total.inc(count)
            except Exception:
                pass

            # Vehicle detections
            try:
                veh_stream = f"detections:vehicle:{ctx.CAMERA_ID}"
                count, _last_veh_id = _count_new_entries(
                    r, veh_stream, _last_veh_id)
                if count > 0:
                    vl_vehicle_detections_total.inc(count)
            except Exception:
                pass

            # Frames/sec — compute directly from stream timestamps
            # Gets first and last entry timestamps, divides stream
            # length by time span. Works regardless of MAXLEN.
            try:
                first = r.xrange(ctx.FRAME_STREAM, count=1)
                last = r.xrevrange(ctx.FRAME_STREAM, count=1)
                if first and last:
                    f_len = r.xlen(ctx.FRAME_STREAM)
                    first_ts = int(first[0][0].split("-")[0]) / 1000.0
                    last_ts = int(last[0][0].split("-")[0]) / 1000.0
                    span = last_ts - first_ts
                    if span > 0:
                        vl_frames_per_second.set(round(f_len / span, 2))
            except Exception:
                pass

            # ------ Latest inference time ------
            try:
                entries = r.xrevrange(ctx.DETECTION_STREAM, count=1)
                if entries:
                    ms = float(entries[0][1].get("inference_ms", "0"))
                    vl_inference_ms.set(ms)
            except Exception:
                pass

            # ------ Active persons ------
            try:
                state_raw = r.get(ctx.STATE_KEY)
                if state_raw:
                    state = json.loads(state_raw)
                    vl_active_persons.set(len(state.get("persons", [])))
                else:
                    vl_active_persons.set(0)
            except Exception:
                pass

            # ------ Event counting (by type) ------
            try:
                new_events = r.xrange(ctx.EVENT_STREAM, min=_last_event_id, count=500)
                for eid, data in new_events:
                    if eid == _last_event_id:
                        continue
                    event_type = data.get("event_type", "unknown")
                    vl_events_total.labels(event_type=event_type).inc()
                    _last_event_id = eid
            except Exception:
                pass

            # ------ GPU pause ------
            try:
                paused = r.exists("gpu:generation_active")
                vl_gpu_pause_active.set(1 if paused else 0)
            except Exception:
                pass

        except Exception as e:
            logger.error(f"Metrics collector error: {e}")

        await asyncio.sleep(10)
