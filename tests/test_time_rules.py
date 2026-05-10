"""
tests/test_time_rules.py — Tests for time period classification and zone geometry.

Tests:
    - get_time_period() returns valid period strings
    - should_alert() with each alert level × time period combination
    - point_in_polygon() with various polygon shapes and edge cases
    - _get_sun_times() returns valid sunrise/sunset times
"""

import sys
import os
import pytest
from datetime import datetime
from zoneinfo import ZoneInfo

# Add project root and contracts to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "contracts"))

from time_rules import (
    get_time_period,
    should_alert,
    point_in_polygon,
    _get_sun_times,
    TIMEZONE,
)


# ===========================================================================
# Point-in-Polygon Tests
# ===========================================================================

class TestPointInPolygon:
    """Tests for the ray-casting point-in-polygon algorithm."""

    # --- Simple square ---
    SQUARE = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]

    def test_center_of_square(self):
        """Point at center of unit square is inside."""
        assert point_in_polygon(0.5, 0.5, self.SQUARE) is True

    def test_outside_square(self):
        """Point clearly outside is outside."""
        assert point_in_polygon(2.0, 2.0, self.SQUARE) is False

    def test_outside_left(self):
        assert point_in_polygon(-0.1, 0.5, self.SQUARE) is False

    def test_outside_above(self):
        assert point_in_polygon(0.5, -0.1, self.SQUARE) is False

    def test_inside_near_edge(self):
        """Point just inside the edge is inside."""
        assert point_in_polygon(0.01, 0.01, self.SQUARE) is True

    def test_outside_near_edge(self):
        """Point just outside the edge is outside."""
        assert point_in_polygon(1.01, 0.5, self.SQUARE) is False

    # --- Triangle ---
    TRIANGLE = [[0.5, 0.0], [1.0, 1.0], [0.0, 1.0]]

    def test_inside_triangle(self):
        assert point_in_polygon(0.5, 0.7, self.TRIANGLE) is True

    def test_outside_triangle(self):
        """Point in bounding box but outside triangle."""
        assert point_in_polygon(0.1, 0.1, self.TRIANGLE) is False

    # --- L-shaped polygon ---
    L_SHAPE = [
        [0.0, 0.0], [0.5, 0.0], [0.5, 0.5],
        [1.0, 0.5], [1.0, 1.0], [0.0, 1.0],
    ]

    def test_inside_l_bottom(self):
        """Point in the bottom part of the L."""
        assert point_in_polygon(0.25, 0.25, self.L_SHAPE) is True

    def test_inside_l_right(self):
        """Point in the right arm of the L."""
        assert point_in_polygon(0.75, 0.75, self.L_SHAPE) is True

    def test_outside_l_notch(self):
        """Point in the notch (top-right cutout) is outside."""
        assert point_in_polygon(0.75, 0.25, self.L_SHAPE) is False

    # --- Degenerate cases ---
    def test_too_few_points(self):
        """Polygon with < 3 points always returns False."""
        assert point_in_polygon(0.5, 0.5, [[0.0, 0.0], [1.0, 1.0]]) is False

    def test_empty_polygon(self):
        assert point_in_polygon(0.5, 0.5, []) is False

    def test_single_point(self):
        assert point_in_polygon(0.0, 0.0, [[0.0, 0.0]]) is False

    # --- Normalized camera coords (realistic zone) ---
    DOORWAY = [[0.1, 0.3], [0.4, 0.3], [0.4, 0.9], [0.1, 0.9]]

    def test_inside_doorway_zone(self):
        """Person bbox center at (0.25, 0.6) is inside the doorway zone."""
        assert point_in_polygon(0.25, 0.6, self.DOORWAY) is True

    def test_outside_doorway_zone(self):
        """Person at (0.8, 0.5) is outside the doorway zone."""
        assert point_in_polygon(0.8, 0.5, self.DOORWAY) is False

    def test_bottom_of_doorway(self):
        """Person at bottom of doorway is inside."""
        assert point_in_polygon(0.25, 0.85, self.DOORWAY) is True

    def test_above_doorway(self):
        """Person above the doorway zone is outside."""
        assert point_in_polygon(0.25, 0.1, self.DOORWAY) is False


# ===========================================================================
# should_alert() Tests
# ===========================================================================

class TestShouldAlert:
    """Tests for the alert decision function."""

    def test_always_alerts_during_day(self):
        assert should_alert("always", "daytime") is True

    def test_always_alerts_at_night(self):
        assert should_alert("always", "night") is True

    def test_always_alerts_late_night(self):
        assert should_alert("always", "late_night") is True

    def test_always_alerts_during_twilight(self):
        assert should_alert("always", "twilight") is True

    def test_night_only_alerts_at_night(self):
        assert should_alert("night_only", "night") is True

    def test_night_only_alerts_at_late_night(self):
        assert should_alert("night_only", "late_night") is True

    def test_night_only_does_not_alert_during_day(self):
        assert should_alert("night_only", "daytime") is False

    def test_night_only_does_not_alert_during_twilight(self):
        assert should_alert("night_only", "twilight") is False

    def test_log_only_never_alerts(self):
        assert should_alert("log_only", "night") is False
        assert should_alert("log_only", "daytime") is False

    def test_ignore_never_alerts(self):
        assert should_alert("ignore", "night") is False
        assert should_alert("ignore", "daytime") is False

    def test_unknown_level_defaults_to_false(self):
        assert should_alert("banana", "night") is False


# ===========================================================================
# get_time_period() Tests
# ===========================================================================

class TestGetTimePeriod:
    """Tests for time period classification."""

    def test_returns_valid_string(self):
        """get_time_period() always returns a valid period."""
        result = get_time_period()
        assert result in ("daytime", "twilight", "night", "late_night")

    def test_midday_is_daytime(self):
        """Noon should be daytime (unless polar regions)."""
        noon = datetime(2026, 6, 15, 12, 0, tzinfo=TIMEZONE)
        assert get_time_period(noon) == "daytime"

    def test_midnight_is_late_night(self):
        """Midnight should be late_night."""
        midnight = datetime(2026, 6, 15, 0, 30, tzinfo=TIMEZONE)
        assert get_time_period(midnight) == "late_night"

    def test_3am_is_late_night(self):
        """3 AM should be late_night."""
        three_am = datetime(2026, 6, 15, 3, 0, tzinfo=TIMEZONE)
        assert get_time_period(three_am) == "late_night"

    def test_11pm_is_night(self):
        """11 PM should be night."""
        eleven_pm = datetime(2026, 6, 15, 23, 0, tzinfo=TIMEZONE)
        assert get_time_period(eleven_pm) == "night"

    def test_2pm_is_daytime(self):
        """2 PM should be daytime."""
        two_pm = datetime(2026, 6, 15, 14, 0, tzinfo=TIMEZONE)
        assert get_time_period(two_pm) == "daytime"


# ===========================================================================
# Sun Times Tests
# ===========================================================================

class TestSunTimes:
    """Tests for sunrise/sunset calculation."""

    def test_sun_times_returns_both(self):
        """_get_sun_times returns dict with sunrise and sunset."""
        now = datetime(2026, 6, 15, 12, 0, tzinfo=TIMEZONE)
        result = _get_sun_times(now)
        assert "sunrise" in result
        assert "sunset" in result

    def test_sunrise_before_sunset(self):
        """Sunrise should always be before sunset."""
        now = datetime(2026, 6, 15, 12, 0, tzinfo=TIMEZONE)
        result = _get_sun_times(now)
        assert result["sunrise"] < result["sunset"]

    def test_summer_sunrise_is_early(self):
        """In June at a northern location, sunrise should be before 7 AM."""
        now = datetime(2026, 6, 15, 12, 0, tzinfo=TIMEZONE)
        result = _get_sun_times(now)
        assert result["sunrise"].hour < 7

    def test_winter_sunset_is_early(self):
        """In December at a northern location, sunset should be before 6 PM."""
        now = datetime(2026, 12, 15, 12, 0, tzinfo=TIMEZONE)
        result = _get_sun_times(now)
        assert result["sunset"].hour < 18
