"""tracker/core/_vehicle_events.py — VehicleEventsMixin.

Extracted from manager.py during the 2026-05-22 mixin split.

Mixed into PersonTracker. Owns the five vehicle-track event emitters
(detected / sample / idle / left / gone) plus their per-sample HD
snapshot writes that vehicle-attributes-cam{N} consumes.
"""

import json

from .config import (
    logger,
    CAMERA_ID,
    EVENT_STREAM,
    MAX_EVENT_STREAM_LEN,
    VEHICLE_SAMPLE_EVENT,
    VEHICLE_GONE_EVENT,
    should_alert,
    get_time_period,
)
from .state import TrackedVehicle  # noqa: F401  (used in forward-ref type hints)


class VehicleEventsMixin:
    """Vehicle-track event emit + per-sample HD-snapshot writes."""

    def _emit_vehicle_detected_event(self, veh: 'TrackedVehicle', timestamp: float):
        """Emit a vehicle_detected event to the events stream."""
        zone_name, alert_level = self._find_zone(veh.bbox)
        alert_triggered = should_alert(alert_level) if alert_level else False

        event = {
            "camera_id": CAMERA_ID,
            "event_type": "vehicle_detected",
            "timestamp": str(timestamp),
            "person_id": "",
            "identity_name": "",
            "duration": "0",
            "direction": "",
            "action": "",
            "bbox": json.dumps(veh.bbox),
            "frame_count": str(veh.frame_count),
            "zone": zone_name,
            "alert_level": alert_level,
            "alert_triggered": str(alert_triggered),
            "vehicle_class": veh.class_name,
            "vehicle_confidence": str(round(veh.confidence, 3)),
            "vehicle_id": veh.vehicle_id,
            "vehicle_first_seen": str(int(veh.first_seen)),
            "snapshot_key": veh.snapshot_key,
            "snapshot_bbox": json.dumps(veh.snapshot_bbox),
            "time_period": get_time_period(),
        }

        self.r.xadd(EVENT_STREAM, event, maxlen=MAX_EVENT_STREAM_LEN)
        self.total_events += 1

        logger.info(
            f"EVENT: vehicle_detected | {veh.class_name} ({veh.confidence:.2f})"
            f"{f' | zone={zone_name}' if zone_name else ''}"
        )

    def _emit_vehicle_sample_event(self, veh: 'TrackedVehicle', timestamp: float):
        """Emit a low-weight sampling event the attribute service uses to
        decide when to crop the current HD frame for this track.

        Mirrors vehicle_detected payload so a consumer can treat both as
        `(track_id, bbox, timestamp)` carriers without branching on event_type.
        """
        # Write the per-sample HD snapshot to Redis with a short TTL so
        # vehicle-attributes-cam{N} crops a frame that's temporally paired
        # with the bbox (avoids the drift bug where `frame_hd:{cam}` may
        # carry a frame from a different moment than when the bbox was
        # computed). Mirror of the v0.2.0 person_appeared snapshot fix.
        hd_snapshot_key = ""
        if veh.last_hd_frame_bytes:
            ts_ms = int(timestamp * 1000)
            hd_snapshot_key = f"vehicle_hd_sample:{CAMERA_ID}:{veh.vehicle_id}:{ts_ms}"
            try:
                self.r.setex(hd_snapshot_key, 60, veh.last_hd_frame_bytes)
            except Exception:
                hd_snapshot_key = ""  # SETEX failed; consumer falls back

        event = {
            "camera_id": CAMERA_ID,
            "event_type": VEHICLE_SAMPLE_EVENT,
            "timestamp": str(timestamp),
            "bbox": json.dumps(veh.bbox),
            "vehicle_class": veh.class_name,
            "vehicle_confidence": str(round(veh.confidence, 3)),
            "vehicle_id": veh.vehicle_id,
            "vehicle_first_seen": str(int(veh.first_seen)),
            "frame_count": str(veh.frame_count),
            "hd_snapshot_key": hd_snapshot_key,
        }
        self.r.xadd(EVENT_STREAM, event, maxlen=MAX_EVENT_STREAM_LEN)

    def _emit_vehicle_idle_event(self, veh: 'TrackedVehicle', timestamp: float):
        """
        Emit a vehicle_idle event when a vehicle has been stationary
        for longer than VEHICLE_IDLE_TIMEOUT.
        """
        zone_name, alert_level = self._find_zone(veh.bbox)
        alert_triggered = should_alert(alert_level) if alert_level else False

        event = {
            "camera_id": CAMERA_ID,
            "event_type": "vehicle_idle",
            "timestamp": str(timestamp),
            "person_id": "",
            "identity_name": "",
            "duration": str(round(veh.duration, 1)),
            "direction": "",
            "action": "",
            "bbox": json.dumps(veh.bbox),
            "frame_count": str(veh.frame_count),
            "zone": zone_name,
            "alert_level": alert_level,
            "alert_triggered": str(alert_triggered),
            "vehicle_class": veh.class_name,
            "vehicle_confidence": str(round(veh.confidence, 3)),
            "vehicle_id": veh.vehicle_id,
            "vehicle_first_seen": str(int(veh.first_seen)),
            "snapshot_key": veh.snapshot_key,
            "snapshot_bbox": json.dumps(veh.snapshot_bbox),
            "time_period": get_time_period(),
        }

        self.r.xadd(EVENT_STREAM, event, maxlen=MAX_EVENT_STREAM_LEN)
        self.total_events += 1

        logger.info(
            f"EVENT: vehicle_idle | {veh.class_name} idling {veh.duration:.1f}s"
            f"{f' | zone={zone_name}' if zone_name else ''}"
        )

    def _emit_vehicle_left_event(self, veh: 'TrackedVehicle', timestamp: float):
        """Emit a vehicle_left event when a tracked vehicle disappears.

        Fires when the vehicle hasn't been detected for VEHICLE_LOST_TIMEOUT
        seconds. `duration` is the full visit length (first_seen → last_seen),
        which is what the dashboard / Telegram feed will display for the
        "car parked here for 2h" use case.
        """
        zone_name, alert_level = self._find_zone(veh.bbox)
        alert_triggered = should_alert(alert_level) if alert_level else False
        visit_duration = veh.last_seen - veh.first_seen

        event = {
            "camera_id": CAMERA_ID,
            "event_type": "vehicle_left",
            "timestamp": str(timestamp),
            "person_id": "",
            "identity_name": "",
            "duration": str(round(visit_duration, 1)),
            "direction": "",
            "action": "",
            "bbox": json.dumps(veh.bbox),
            "frame_count": str(veh.frame_count),
            "zone": zone_name,
            "alert_level": alert_level,
            "alert_triggered": str(alert_triggered),
            "vehicle_class": veh.class_name,
            "vehicle_confidence": str(round(veh.confidence, 3)),
            "vehicle_id": veh.vehicle_id,
            "vehicle_first_seen": str(int(veh.first_seen)),
            "snapshot_key": veh.snapshot_key,
            "snapshot_bbox": json.dumps(veh.snapshot_bbox),
            "time_period": get_time_period(),
        }

        self.r.xadd(EVENT_STREAM, event, maxlen=MAX_EVENT_STREAM_LEN)
        self.total_events += 1

        logger.info(
            f"EVENT: vehicle_left | {veh.class_name} after {visit_duration:.1f}s"
            f"{f' | zone={zone_name}' if zone_name else ''}"
        )

    def _emit_vehicle_gone_event(self, veh: 'TrackedVehicle', timestamp: float):
        """Emit a vehicle_gone event when a tracked vehicle's ghost expires.

        Fires for EVERY track end — both drive-bys and idle-leaves. Used by
        `vehicle-attributes-cam{N}` to flush its per-track HD-crop buffer
        regardless of whether the vehicle ever went idle. Carries `was_idle`
        so consumers can distinguish without re-deriving from `duration`.

        Drive-by tracks (short duration, never set idle_alerted): only
        vehicle_gone fires. Idle-leave tracks: BOTH vehicle_gone (internal)
        AND vehicle_left (user-facing) fire.
        """
        visit_duration = veh.last_seen - veh.first_seen

        event = {
            "camera_id": CAMERA_ID,
            "event_type": VEHICLE_GONE_EVENT,
            "timestamp": str(timestamp),
            "duration": str(round(visit_duration, 1)),
            "bbox": json.dumps(veh.bbox),
            "frame_count": str(veh.frame_count),
            "vehicle_class": veh.class_name,
            "vehicle_confidence": str(round(veh.confidence, 3)),
            "vehicle_id": veh.vehicle_id,
            "vehicle_first_seen": str(int(veh.first_seen)),
            "was_idle": str(bool(veh.idle_alerted)),
        }

        self.r.xadd(EVENT_STREAM, event, maxlen=MAX_EVENT_STREAM_LEN)
        self.total_events += 1
