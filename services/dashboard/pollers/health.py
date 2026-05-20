"""
services/dashboard/pollers/health.py — disk + Redis memory alerts.

PURPOSE:
    Watch the two resources that can quietly kill the stack and alert via
    Telegram BEFORE they fill up:

    - /data disk usage (recordings, snapshots, events, face DB, auth DB).
      If this hits 100% the recorder ffmpeg writes fail, the retention
      poller deletes nothing because mtime checks need the FS healthy,
      and the auth/face SQLite DBs error on the next commit.

    - Redis memory vs maxmemory. When AOF + streams approach the 2GB cap,
      `maxmemory-policy allkeys-lru` quietly evicts data the dashboard
      depends on (face embeddings, event history, config hashes) without
      surfacing any error to the user.

ALERTING:
    Hysteresis to prevent spam: alert when usage crosses the WARN threshold
    (85%); clear-alert ("recovered") when it falls below the CLEAR threshold
    (75%). One alert per crossing, not per cycle.

    Telegram broadcast goes through the standard notifications.broadcast_text
    so it lands on every approved user.

CADENCE:
    60s — fast enough to catch a runaway log spike before the disk fills,
    slow enough not to burn cycles. Wakes once at boot (after 30s grace),
    then every 60s.
"""

import asyncio
import logging
import os
import shutil
import time

logger = logging.getLogger("dashboard.health")

# Thresholds (env-tunable, but defaults are sane)
DISK_WARN_PCT = float(os.getenv("DISK_WARN_PCT", "85"))
DISK_CLEAR_PCT = float(os.getenv("DISK_CLEAR_PCT", "75"))
REDIS_WARN_PCT = float(os.getenv("REDIS_WARN_PCT", "85"))
REDIS_CLEAR_PCT = float(os.getenv("REDIS_CLEAR_PCT", "75"))

# Path to monitor. /data is the bind mount that holds recordings, snapshots,
# events, face DB, auth DB. If this dir doesn't exist (e.g. tests, dev),
# the poller silently skips disk checks.
DISK_PATH = os.getenv("HEALTH_DISK_PATH", "/data")
CYCLE_SECONDS = int(os.getenv("HEALTH_POLLER_CYCLE_SECS", "60"))


async def health_poller():
    """Loop forever, alerting on disk/Redis pressure crossings."""
    # Hysteresis state: True when we've fired a warning that hasn't cleared yet.
    disk_alert_active = False
    redis_alert_active = False

    logger.info(
        f"Health poller starting — cycle {CYCLE_SECONDS}s, "
        f"disk warn>{DISK_WARN_PCT}% clear<{DISK_CLEAR_PCT}%, "
        f"redis warn>{REDIS_WARN_PCT}% clear<{REDIS_CLEAR_PCT}% (path={DISK_PATH})"
    )

    # Grace period — let the rest of startup settle so we don't alert mid-boot
    await asyncio.sleep(30)

    # Lazy imports — avoid blocking startup if these modules pull in deps
    from routes.notifications import broadcast_text, is_configured
    import routes as ctx

    while True:
        try:
            # --- Disk check ---
            if os.path.isdir(DISK_PATH):
                try:
                    usage = shutil.disk_usage(DISK_PATH)
                    pct = (usage.used / usage.total) * 100 if usage.total else 0
                    if pct >= DISK_WARN_PCT and not disk_alert_active:
                        disk_alert_active = True
                        msg = (
                            f"\U0001f4be <b>Disk Usage High</b>\n"
                            f"• {DISK_PATH}: <b>{pct:.1f}% used</b> "
                            f"({usage.used / 1e9:.1f} / {usage.total / 1e9:.1f} GB)\n"
                            f"• At 100%, DVR recording stops and snapshot writes fail.\n"
                            f"• Free space by lowering SNAPSHOT_RETENTION_DAYS / RETENTION_DAYS "
                            f"in setup, or delete old recordings from the DVR tab."
                        )
                        if is_configured():
                            try:
                                await broadcast_text(msg)
                            except Exception as e:
                                logger.warning(f"Disk-alert broadcast failed: {e}")
                        logger.warning(f"Disk usage alert: {pct:.1f}%")
                    elif pct < DISK_CLEAR_PCT and disk_alert_active:
                        disk_alert_active = False
                        msg = (
                            f"✅ <b>Disk Usage Recovered</b>\n"
                            f"• {DISK_PATH}: now <b>{pct:.1f}% used</b>"
                        )
                        if is_configured():
                            try:
                                await broadcast_text(msg)
                            except Exception:
                                pass
                        logger.info(f"Disk usage cleared: {pct:.1f}%")
                except Exception as e:
                    logger.warning(f"Disk check failed: {e}")

            # --- Redis memory check ---
            try:
                info = ctx.r.info("memory")
                used = int(info.get("used_memory", 0))
                max_mem = int(info.get("maxmemory", 0))
                if max_mem > 0:
                    pct = (used / max_mem) * 100
                    if pct >= REDIS_WARN_PCT and not redis_alert_active:
                        redis_alert_active = True
                        msg = (
                            f"\U0001f9e0 <b>Redis Memory High</b>\n"
                            f"• Used: <b>{pct:.1f}%</b> "
                            f"({used / 1e6:.1f} / {max_mem / 1e6:.0f} MB)\n"
                            f"• At 100%, allkeys-lru policy starts evicting "
                            f"event history, face embeddings, and config hashes "
                            f"silently — features will degrade with no error.\n"
                            f"• Consider raising maxmemory in docker-compose.yml "
                            f"or lowering MAX_EVENT_STREAM_LEN."
                        )
                        if is_configured():
                            try:
                                await broadcast_text(msg)
                            except Exception as e:
                                logger.warning(f"Redis-alert broadcast failed: {e}")
                        logger.warning(f"Redis memory alert: {pct:.1f}%")
                    elif pct < REDIS_CLEAR_PCT and redis_alert_active:
                        redis_alert_active = False
                        msg = (
                            f"✅ <b>Redis Memory Recovered</b>\n"
                            f"• Now <b>{pct:.1f}%</b> "
                            f"({used / 1e6:.1f} / {max_mem / 1e6:.0f} MB)"
                        )
                        if is_configured():
                            try:
                                await broadcast_text(msg)
                            except Exception:
                                pass
                        logger.info(f"Redis memory cleared: {pct:.1f}%")
                # If maxmemory=0 (unlimited), Redis has no cap configured — skip
            except Exception as e:
                logger.warning(f"Redis memory check failed: {e}")
        except Exception as e:
            logger.warning(f"Health poller error: {e}")

        await asyncio.sleep(CYCLE_SECONDS)
