"""
contracts/time_rules.py — Time-of-day classification for zone alert rules.

PURPOSE:
    Determines the current time period (daytime, twilight, night, late_night)
    based on sunrise/sunset calculations for the camera's location.
    Used by the tracker to decide whether a zone should trigger an alert.

RELATIONSHIPS:
    - Used by: services/tracker/tracker.py (evaluates zone rules)
    - Location: configured via env vars (LOCATION_LAT, LOCATION_LON, etc.)

TIME PERIODS:
    - daytime:    sunrise + 30min  →  sunset - 30min
    - twilight:   30 min before/after sunrise and sunset
    - night:      sunset + 30min  →  midnight
    - late_night: midnight  →  sunrise - 30min

ALERT LEVELS (per zone):
    - "always":     alert in all time periods
    - "night_only": alert only during night + late_night
    - "log_only":   never alert, just log
    - "ignore":     skip alerting, still track
    - "dead_zone":  completely suppress — no tracking, no events, no bbox
"""

import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

try:
    from astral import LocationInfo
    from astral.sun import sun
    HAS_ASTRAL = True
except ImportError:
    HAS_ASTRAL = False

# Location configured via environment variables — no personal data in source
LOCATION = {
    "name": os.getenv("LOCATION_NAME", "Default"),
    "region": os.getenv("LOCATION_REGION", ""),
    "timezone": os.getenv("LOCATION_TIMEZONE", "America/Toronto"),
    "latitude": float(os.getenv("LOCATION_LAT", "43.6532")),
    "longitude": float(os.getenv("LOCATION_LON", "-79.3832")),
}

TIMEZONE = ZoneInfo(LOCATION["timezone"])

# Twilight buffer around sunrise/sunset
TWILIGHT_MINUTES = 30


def _get_sun_times(date: datetime = None) -> dict:
    """
    Get sunrise and sunset times for the configured location on the given date.

    Returns dict with 'sunrise' and 'sunset' as timezone-aware datetimes.
    Falls back to fixed times if astral is not installed.
    """
    if date is None:
        date = datetime.now(TIMEZONE)

    if HAS_ASTRAL:
        loc = LocationInfo(
            LOCATION["name"],
            LOCATION["region"],
            LOCATION["timezone"],
            LOCATION["latitude"],
            LOCATION["longitude"],
        )
        s = sun(loc.observer, date=date.date(), tzinfo=TIMEZONE)
        return {"sunrise": s["sunrise"], "sunset": s["sunset"]}
    else:
        # Fallback: approximate times for Eastern timezone
        d = date.date()
        return {
            "sunrise": datetime(d.year, d.month, d.day, 7, 0, tzinfo=TIMEZONE),
            "sunset": datetime(d.year, d.month, d.day, 19, 0, tzinfo=TIMEZONE),
        }


def get_time_period(now: datetime = None) -> str:
    """
    Classify the current time into a period.

    Returns one of: "daytime", "twilight", "night", "late_night"
    """
    if now is None:
        now = datetime.now(TIMEZONE)

    sun_times = _get_sun_times(now)
    sunrise = sun_times["sunrise"]
    sunset = sun_times["sunset"]

    buffer = timedelta(minutes=TWILIGHT_MINUTES)

    # Late night: midnight → sunrise - buffer
    if now.hour < sunrise.hour or (now < sunrise - buffer):
        return "late_night"

    # Morning twilight: sunrise - buffer → sunrise + buffer
    if sunrise - buffer <= now <= sunrise + buffer:
        return "twilight"

    # Daytime: sunrise + buffer → sunset - buffer
    if sunrise + buffer < now < sunset - buffer:
        return "daytime"

    # Evening twilight: sunset - buffer → sunset + buffer
    if sunset - buffer <= now <= sunset + buffer:
        return "twilight"

    # Night: sunset + buffer → midnight
    return "night"


def should_alert(alert_level: str, time_period: str = None) -> bool:
    """
    Determine whether a zone should trigger an alert right now.

    Args:
        alert_level: Zone's configured alert level
                     ("always", "night_only", "log_only", "ignore")
        time_period: Override time period (if None, computed from current time)

    Returns:
        True if the zone should generate an alert.
    """
    if alert_level == "ignore":
        return False

    if alert_level == "log_only":
        return False

    if alert_level == "always":
        return True

    if time_period is None:
        time_period = get_time_period()

    if alert_level == "night_only":
        return time_period in ("night", "late_night")

    # Unknown alert level — default to log only
    return False


def point_in_polygon(px: float, py: float, polygon: list[list[float]]) -> bool:
    """
    Ray-casting algorithm to check if point (px, py) is inside a polygon.

    Args:
        px, py: Point coordinates (normalized 0-1)
        polygon: List of [x, y] vertices (normalized 0-1)

    Returns:
        True if the point is inside the polygon.
    """
    n = len(polygon)
    if n < 3:
        return False

    inside = False
    j = n - 1

    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]

        if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / (yj - yi) + xi):
            inside = not inside

        j = i

    return inside
