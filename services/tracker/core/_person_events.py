"""tracker/core/_person_events.py — PersonEventsMixin.

Extracted from manager.py during the 2026-05-22 mixin split.

Mixed into PersonTracker. Owns the events stream emit for person tracks
plus the helper that pairs a bbox with the JPEG frame it came from so
the notification draws over the right pixels.
"""

import json

from .config import (
    logger,
    CAMERA_ID,
    EVENT_STREAM,
    FRAME_STREAM,
    PERSON_SNAPSHOT_KEY_TMPL as _PSNAP_TMPL,
    MAX_EVENT_STREAM_LEN,
    stream_key,
    should_alert,
    get_time_period,
)
from .state import TrackedPerson


class PersonEventsMixin:
    """Person-track event emit + companion snapshot key writes."""

    def _emit_event(self, event_type: str, person: TrackedPerson, timestamp: float, extra: dict = None):
        """Publish an event to the events stream."""
        # Determine which zone the person is in
        zone_name, alert_level = self._find_zone(person.bbox)

        # Evaluate zone + time-of-day rules to decide if this should trigger an alert
        alert_triggered = should_alert(alert_level) if alert_level else False

        event = {
            "camera_id": CAMERA_ID,
            "event_type": event_type,
            "timestamp": str(timestamp),
            "person_id": person.person_id,
            "identity_name": person.identity_name,
            "duration": str(round(person.duration, 1)),
            "direction": person.direction,
            "action": person.action,
            "bbox": json.dumps(person.bbox),
            "frame_count": str(person.frame_count),
            "zone": zone_name,
            "alert_level": alert_level,
            "alert_triggered": str(alert_triggered),
            "time_period": get_time_period(),
        }

        # Save a snapshot at event emission time for person events
        # so the dashboard uses the correct frame, not the (stale) live frame.
        # Also save the bbox that matches the snapshot frame so the dashboard
        # draws the box in the right place (not a stale detection bbox).
        if event_type in ("person_appeared", "person_identified"):
            # Pass the buffered frame_bytes (paired with `person.bbox` at
            # detection time) so the snapshot frame matches the bbox. Falls
            # back to grabbing the latest frame if we don't have buffered
            # bytes (e.g. pose-detector hasn't been upgraded yet).
            snap_key = self._save_person_snapshot(
                timestamp, person.bbox, frame_bytes=person.last_frame_bytes,
            )
            if snap_key:
                event["snapshot_key"] = snap_key
                event["snapshot_bbox"] = json.dumps(person.bbox)

        if extra:
            event.update(extra)

        self.r.xadd(EVENT_STREAM, event, maxlen=MAX_EVENT_STREAM_LEN)
        self.total_events += 1

        zone_str = f" | zone={zone_name}" if zone_name else ""
        name_str = f" ({person.identity_name})" if person.identity_name else ""
        logger.info(
            f"EVENT: {event_type} | {person.person_id}{name_str} | "
            f"action={person.action} | "
            f"duration={person.duration:.1f}s | direction={person.direction}"
            f"{zone_str}"
        )

    def _save_person_snapshot(self, timestamp: float, bbox: list = None,
                                frame_bytes: bytes | None = None) -> str | None:
        """
        Save the JPEG frame that the bbox was computed from, plus the bbox
        itself, to Redis so the dashboard can render the notification with
        the box drawn over the right pixels.

        `frame_bytes` is the preferred input — passed in from
        TrackedPerson.last_frame_bytes, which is the exact frame the
        pose-detector ran on when it produced `bbox`. With the 4-second
        announce grace period there's a wide gap between detection time
        and emit time, so falling back to "latest frame in the stream"
        (the old behavior, kept as a defensive fallback) draws the bbox
        on a frame from several seconds AFTER the detection. For a moving
        person that gap is the "bbox on empty floor" symptom.

        Returns the Redis key or None if no frame available.
        Uses 2h TTL (matches dashboard snapshot cleanup).
        """
        try:
            # Prefer the frame bytes paired with this detection. Fall back
            # to xrevrange only if the caller didn't pass any (older
            # detector versions, or pre-detection event types).
            if not frame_bytes:
                entries = self.r.xrevrange(FRAME_STREAM.encode(), count=1)
                if entries:
                    frame_bytes = entries[0][1].get(b"frame") or entries[0][1].get(b"frame_bytes")
            if not frame_bytes:
                return None

            snap_key = stream_key(_PSNAP_TMPL, camera_id=CAMERA_ID, timestamp=int(timestamp))
            self.r.setex(snap_key, 7200, frame_bytes)  # 2h TTL

            # Save companion bbox key so dashboard draws box in the right place
            if bbox:
                bbox_key = f"{snap_key}:bbox"
                self.r.setex(bbox_key, 7200, json.dumps(bbox))

            return snap_key
        except Exception as e:
            logger.debug(f"Person snapshot save failed: {e}")
            return None
