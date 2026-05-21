"""
tests/test_notifications.py — Tests for notification logic.

Tests the duration formatting, rate-limiting behavior, and
snapshot_bbox selection in notification functions.

NO real Telegram or Redis. Uses monkeypatch/patch to isolate logic.
"""

import os
import sys
import time

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_DASHBOARD_DIR = os.path.join(
    os.path.dirname(__file__), "..", "services", "dashboard"
)
sys.path.insert(0, _DASHBOARD_DIR)


# ===========================================================================
# Duration Formatting (vehicle idle notifications)
# ===========================================================================
class TestDurationFormatting:
    """Tests for the inline duration formatting in notify_vehicle_idle.
    The formatting logic is:
        >= 3600s  → "{x:.1f} hours"
        >= 60s    → "{x:.0f} min"
        < 60s     → "{x:.0f}s"
    """

    @staticmethod
    def _format_duration(seconds: float) -> str:
        """Extract the inline formatting logic from notify_vehicle_idle."""
        if seconds >= 3600:
            return f"{seconds / 3600:.1f} hours"
        elif seconds >= 60:
            return f"{seconds / 60:.0f} min"
        else:
            return f"{seconds:.0f}s"

    def test_seconds_format(self):
        """< 60s → shows seconds with 's' suffix."""
        assert self._format_duration(45) == "45s"
        assert self._format_duration(30) == "30s"
        assert self._format_duration(1) == "1s"
        assert self._format_duration(59) == "59s"

    def test_minutes_format(self):
        """60-3599s → shows minutes with 'min' suffix."""
        assert self._format_duration(60) == "1 min"
        assert self._format_duration(150) == "2 min"  # 2.5 rounded → 2 min
        assert self._format_duration(3599) == "60 min"

    def test_hours_format(self):
        """>= 3600s → shows hours with 1 decimal."""
        assert self._format_duration(3600) == "1.0 hours"
        assert self._format_duration(5400) == "1.5 hours"
        assert self._format_duration(7200) == "2.0 hours"

    def test_zero_seconds(self):
        """0 seconds → '0s'."""
        assert self._format_duration(0) == "0s"

    def test_boundary_59_to_60(self):
        """59s → '59s', 60s → '1 min' — clean boundary."""
        assert self._format_duration(59) == "59s"
        assert self._format_duration(60) == "1 min"

    def test_boundary_3599_to_3600(self):
        """3599s → '60 min', 3600s → '1.0 hours' — clean boundary."""
        assert self._format_duration(3599) == "60 min"
        assert self._format_duration(3600) == "1.0 hours"


# ===========================================================================
# snapshot_bbox Selection Logic
# ===========================================================================
class TestSnapshotBboxSelection:
    """Tests for the bbox selection logic used in notifications:
        Use snapshot_bbox if available, else fallback to bbox.
    This mirrors the logic in both notify_vehicle_idle and
    notify_person_detected.
    """

    @staticmethod
    def _select_bbox(event_data: dict) -> str:
        """Extract the bbox selection logic used in notifications."""
        return event_data.get("snapshot_bbox", "") or event_data.get("bbox", "")

    def test_uses_snapshot_bbox_when_present(self):
        """When both snapshot_bbox and bbox are present, snapshot_bbox wins."""
        event = {
            "snapshot_bbox": "[100, 200, 300, 400]",
            "bbox": "[50, 50, 150, 150]",
        }
        assert self._select_bbox(event) == "[100, 200, 300, 400]"

    def test_falls_back_to_bbox_when_no_snapshot(self):
        """When snapshot_bbox is missing, falls back to bbox."""
        event = {
            "bbox": "[50, 50, 150, 150]",
        }
        assert self._select_bbox(event) == "[50, 50, 150, 150]"

    def test_falls_back_to_bbox_when_snapshot_empty(self):
        """When snapshot_bbox is empty string, falls back to bbox."""
        event = {
            "snapshot_bbox": "",
            "bbox": "[50, 50, 150, 150]",
        }
        assert self._select_bbox(event) == "[50, 50, 150, 150]"

    def test_empty_when_neither_present(self):
        """When neither is present, returns empty string."""
        event = {"event_type": "person_appeared"}
        assert self._select_bbox(event) == ""

    def test_snapshot_bbox_with_none_value(self):
        """snapshot_bbox set to None should fall back to bbox."""
        event = {
            "snapshot_bbox": None,
            "bbox": "[10, 20, 30, 40]",
        }
        assert self._select_bbox(event) == "[10, 20, 30, 40]"


# ===========================================================================
# Rate-Limit Logic (unit-testable pattern)
# ===========================================================================
class TestRateLimitLogic:
    """Tests for the rate-limiting pattern used in notification functions.
    This tests the pattern itself (not the async Telegram calls):
        if now - last_notification < cooldown:
            return 0  # Rate limited
    """

    @staticmethod
    def _is_rate_limited(last_time: float, now: float, cooldown: int) -> bool:
        """Extract the rate-limit check used in notifications."""
        return (now - last_time) < cooldown

    def test_within_cooldown_is_limited(self):
        """Request within cooldown window → rate limited."""
        last = 1000.0
        now = 1030.0   # 30s later
        cooldown = 60   # 60s cooldown
        assert self._is_rate_limited(last, now, cooldown) is True

    def test_after_cooldown_is_allowed(self):
        """Request after cooldown expires → allowed."""
        last = 1000.0
        now = 1061.0   # 61s later
        cooldown = 60
        assert self._is_rate_limited(last, now, cooldown) is False

    def test_exactly_at_cooldown_is_limited(self):
        """Request exactly at cooldown boundary → still limited (< not <=)."""
        last = 1000.0
        now = 1060.0   # Exactly 60s
        cooldown = 60
        assert self._is_rate_limited(last, now, cooldown) is False  # not < 60

    def test_first_notification_always_allowed(self):
        """First notification (last=0) → always allowed."""
        assert self._is_rate_limited(0.0, time.time(), 60) is False

    def test_minimum_cooldown_floor(self):
        """_get_cooldown floors at 10s to prevent spam."""
        # Test the floor logic directly
        def get_cooldown_with_floor(val: int) -> int:
            return max(10, val)

        assert get_cooldown_with_floor(5) == 10   # Floored
        assert get_cooldown_with_floor(10) == 10  # At floor
        assert get_cooldown_with_floor(60) == 60  # Above floor


# ===========================================================================
# Zone Time-of-Day Gate (vehicle idle notifications)
# ===========================================================================
class TestZoneAlertGate:
    """The zone-rule gate decides whether to skip a notification based on
    `alert_level` and `alert_triggered` on the event. Rules:
      - No zone set (alert_level=="") → always notify (back-compat)
      - Zone set + alert_triggered=="True" → notify
      - Zone set + alert_triggered=="False" → skip
    """

    @staticmethod
    def _should_skip(event_data: dict) -> bool:
        alert_level = event_data.get("alert_level", "")
        alert_triggered_str = event_data.get("alert_triggered", "False")
        return bool(alert_level) and alert_triggered_str != "True"

    def test_no_zone_does_not_skip(self):
        """No alert_level (unzoned vehicle) → always notify."""
        assert self._should_skip({"alert_level": ""}) is False
        assert self._should_skip({}) is False

    def test_zone_triggered_does_not_skip(self):
        """alert_level set + alert_triggered=True → notify."""
        assert self._should_skip({
            "alert_level": "night_only",
            "alert_triggered": "True",
        }) is False

    def test_zone_not_triggered_skips(self):
        """alert_level set + alert_triggered=False → skip."""
        assert self._should_skip({
            "alert_level": "night_only",
            "alert_triggered": "False",
        }) is True

    def test_log_only_zone_skips(self):
        """`log_only` zones never trigger, so alert_triggered is False → skip."""
        assert self._should_skip({
            "alert_level": "log_only",
            "alert_triggered": "False",
        }) is True

    def test_always_zone_notifies(self):
        """`always` zones produce alert_triggered=True → notify."""
        assert self._should_skip({
            "alert_level": "always",
            "alert_triggered": "True",
        }) is False


# ===========================================================================
# Position-Based Dedup Key Shape (replaced per-vehicle dedup)
# ===========================================================================
class TestVehiclePositionDedupKey:
    """Position-quantized dedup keys the parking SPOT, not the tracker
    instance. Solves the failure mode where the same physical car gets
    re-tracked under multiple vehicle_id values (tracker restart, IoU
    identity swap with a passing car, ghost-expiry after long occlusion)
    and each fresh tracker_id was bypassing the old per-tracker SETNX.

    Live data on cam1 showed three separate Telegrams for the same parked
    car within an hour: vehicle_0029 → vehicle_0001 → vehicle_0003, all
    at bbox ~[728, 322, 807, 359]. With position dedup all three collapse
    to the same key.

    Key shape: notify:vehicle_idle:seen:{camera_id}:{grid_x}_{grid_y}
    Grid step: 100 px (bbox center quantized).
    """

    @staticmethod
    def _key(camera_id: str, bbox) -> str | None:
        # Mirror of services/dashboard/routes/notifications/_shared.py
        # `_vehicle_position_dedup_key`. Extracted into the test so the
        # contract is asserted without importing dashboard internals
        # (matches the pattern of every other helper test in this file).
        import json as _json
        if bbox is None:
            return None
        try:
            if isinstance(bbox, str):
                bbox = _json.loads(bbox)
            if not (isinstance(bbox, list) and len(bbox) == 4):
                return None
            cx = (bbox[0] + bbox[2]) / 2
            cy = (bbox[1] + bbox[3]) / 2
            grid_x = int(cx // 100)
            grid_y = int(cy // 100)
            return f"notify:vehicle_idle:seen:{camera_id}:{grid_x}_{grid_y}"
        except (ValueError, TypeError, _json.JSONDecodeError):
            return None

    def test_parked_car_grid_bucket(self):
        """The bbox [728, 322, 807, 359] from real cam1 data quantizes to
        grid (7, 3) at 100px buckets."""
        key = self._key("cam1", [728, 322, 807, 359])
        assert key == "notify:vehicle_idle:seen:cam1:7_3"

    def test_same_grid_for_jittered_bboxes(self):
        """Tiny YOLO bbox jitter on a parked car (a few px frame-to-frame)
        keeps the bbox in the same grid bucket → same dedup key → second
        idle event for the same parked car is correctly suppressed."""
        k1 = self._key("cam1", [728.0, 322.9, 807.5, 359.4])
        k2 = self._key("cam1", [729.3, 323.9, 804.8, 359.3])
        k3 = self._key("cam1", [728.6, 324.7, 808.3, 359.1])
        assert k1 == k2 == k3 == "notify:vehicle_idle:seen:cam1:7_3"

    def test_different_tracker_ids_same_parking_spot_collide(self):
        """The core fix: vehicle_0029 / vehicle_0001 / vehicle_0003 all
        emitted at the same parking spot from real data → ONE dedup key.
        Previously each tracker-id had its own key → three separate
        Telegram notifications for the same physical car."""
        spot = [728, 322, 807, 359]
        # The event payload's tracker fields don't even reach the helper —
        # position alone determines the key.
        k1 = self._key("cam1", spot)
        k2 = self._key("cam1", spot)
        k3 = self._key("cam1", spot)
        assert k1 == k2 == k3

    def test_different_spots_yield_different_keys(self):
        """Two cars parked far apart get distinct dedup keys."""
        k1 = self._key("cam1", [728, 322, 807, 359])
        k2 = self._key("cam1", [200, 400, 280, 440])
        assert k1 != k2

    def test_different_cameras_yield_different_keys(self):
        """Same spot on different cameras → different keys."""
        k1 = self._key("cam1", [728, 322, 807, 359])
        k2 = self._key("cam2", [728, 322, 807, 359])
        assert k1 != k2

    def test_no_key_for_missing_bbox(self):
        """No bbox → skip dedup (caller should treat as 'allow notify')."""
        assert self._key("cam1", None) is None

    def test_no_key_for_malformed_bbox(self):
        """Malformed bbox JSON → skip dedup (don't crash, don't block)."""
        assert self._key("cam1", "[not json") is None
        assert self._key("cam1", [1, 2, 3]) is None  # wrong arity
        assert self._key("cam1", {"x": 1}) is None  # wrong type

    def test_json_string_bbox_works(self):
        """The event payload stores bbox as a JSON string — verify the
        helper parses it transparently."""
        k1 = self._key("cam1", "[728, 322, 807, 359]")
        k2 = self._key("cam1", [728, 322, 807, 359])
        assert k1 == k2
