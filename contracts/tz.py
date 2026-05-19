"""
contracts/tz.py — Single source of truth for the local timezone.

PURPOSE:
    All date/time math involving "today/yesterday" must agree on the same
    timezone. Twelve files previously each did their own
    `ZoneInfo(os.getenv("LOCATION_TIMEZONE", "America/Toronto"))`, which is
    both repetitive and unsafe (a typo silently falls back to default — so
    "yesterday" queries silently shift).

USAGE:
    from contracts.tz import TZ_LOCAL
    now = datetime.now(TZ_LOCAL)

VALIDATION:
    Importing this module:
      - Resolves LOCATION_TIMEZONE env (default America/Toronto)
      - REFUSES to start the service if the value is not a valid IANA name
        (raises SystemExit(2), the standard exit code for misconfiguration)
      - Logs the resolved zone + current local + UTC times at import so the
        operator can verify in `docker logs` what was actually picked

    Unset env → warns but proceeds with default. A typo → crash. This is
    deliberate: silent fallback was the original bug.
"""

import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger("contracts.tz")

_DEFAULT_TZ = "America/Toronto"
_TZ_ENV = "LOCATION_TIMEZONE"


def _load_tz() -> ZoneInfo:
    raw = os.getenv(_TZ_ENV, "").strip()
    if not raw:
        logger.warning(
            f"{_TZ_ENV} not set — defaulting to {_DEFAULT_TZ}. "
            f"Set this in .env to match your actual location to keep "
            f"'today/yesterday' date queries accurate."
        )
        raw = _DEFAULT_TZ
    try:
        tz = ZoneInfo(raw)
    except ZoneInfoNotFoundError:
        logger.error(
            f"Invalid {_TZ_ENV}={raw!r}. Must be a valid IANA timezone name "
            f"(e.g. 'America/Toronto', 'Europe/London', 'Asia/Tokyo'). "
            f"Refusing to start — fix .env."
        )
        raise SystemExit(2)
    now_local = datetime.now(tz)
    now_utc = datetime.now(timezone.utc)
    logger.info(
        f"Timezone: {raw} (offset {now_local.strftime('%z')}) — "
        f"local now = {now_local.strftime('%Y-%m-%d %H:%M:%S %Z')}, "
        f"UTC now = {now_utc.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    return tz


TZ_LOCAL: ZoneInfo = _load_tz()
