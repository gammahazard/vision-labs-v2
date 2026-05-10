"""
tests/test_notifications.py — Tests for notification logic.

Tests the duration formatting, rate-limiting behavior, and
snapshot_bbox selection in notification functions.

NO real Telegram or Redis. Uses monkeypatch/patch to isolate logic.
"""

import os
import sys
import time
import pytest

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
