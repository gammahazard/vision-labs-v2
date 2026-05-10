"""
services/dashboard/pollers/retention.py — daily prune of local snapshots + events.

PURPOSE:
    When the dashboard is running without the QNAP NAS, snapshots
    (/data/snapshots/*.jpg + /data/snapshots/vehicles/<day>/*) and event
    journals (/data/events/<day>.jsonl) accumulate locally. This poller
    deletes anything older than SNAPSHOT_RETENTION_DAYS (default 4 days)
    once per day.

WHY 4 DAYS DEFAULT:
    Short enough to keep local disk under control on a system with no
    long-term storage; long enough to give the user time to review events
    in the dashboard event feed before they're pruned.

WHEN TO DISABLE:
    Set SNAPSHOT_RETENTION_DAYS=0 (env var). The poller logs once at
    startup and exits. Useful if QNAP retention is the source of truth
    or if you want full local history during testing.

WHAT GETS PRUNED:
    1. `/data/snapshots/*.jpg` — flat person/event snapshots, by mtime
    2. `/data/snapshots/vehicles/YYYY-MM-DD/` — vehicle subfolders, by name
    3. `/data/events/YYYY-MM-DD.jsonl` — event journals, by filename date

CADENCE:
    Once at boot (after a 60s grace period), then every 86400s (24h).
"""

import asyncio
import logging
import os
import shutil
import time
from datetime import datetime, timedelta

logger = logging.getLogger("dashboard.retention")


async def retention_poller():
    """Daily prune of /data/snapshots and /data/events older than the configured retention."""
    retention_days = int(os.getenv("SNAPSHOT_RETENTION_DAYS", "4"))
    if retention_days <= 0:
        logger.info("Local retention disabled (SNAPSHOT_RETENTION_DAYS=0)")
        return

    SNAPSHOT_DIR = os.environ.get("SNAPSHOT_DIR", "/data/snapshots")
    EVENT_DIR = os.environ.get("EVENT_JOURNAL_DIR", "/data/events")

    # Liveness log — proves the poller actually started (was missing pre-refactor)
    logger.info(
        f"Local retention enabled — pruning files older than {retention_days}d "
        f"(snapshots={SNAPSHOT_DIR}, events={EVENT_DIR})"
    )

    await asyncio.sleep(60)  # let the rest of startup finish

    while True:
        try:
            cutoff_ts = time.time() - (retention_days * 86400)
            cutoff_date = (datetime.now() - timedelta(days=retention_days)).date()
            removed_files = 0
            removed_bytes = 0

            # 1. Flat /data/snapshots/*.jpg (person snapshots, written by event poller)
            if os.path.isdir(SNAPSHOT_DIR):
                for entry in os.scandir(SNAPSHOT_DIR):
                    if entry.is_file() and entry.name.endswith(".jpg"):
                        try:
                            st = entry.stat()
                            if st.st_mtime < cutoff_ts:
                                os.remove(entry.path)
                                removed_files += 1
                                removed_bytes += st.st_size
                        except Exception:
                            pass

            # 2. Vehicle snapshots organized as /data/snapshots/vehicles/YYYY-MM-DD/
            vehicles_dir = os.path.join(SNAPSHOT_DIR, "vehicles")
            if os.path.isdir(vehicles_dir):
                for entry in os.scandir(vehicles_dir):
                    if not entry.is_dir():
                        continue
                    try:
                        day = datetime.strptime(entry.name, "%Y-%m-%d").date()
                    except ValueError:
                        continue
                    if day < cutoff_date:
                        try:
                            shutil.rmtree(entry.path)
                            removed_files += 1
                        except Exception:
                            pass

            # 3. Event journals at /data/events/YYYY-MM-DD.jsonl
            if os.path.isdir(EVENT_DIR):
                for entry in os.scandir(EVENT_DIR):
                    if not (entry.is_file() and entry.name.endswith(".jsonl")):
                        continue
                    try:
                        day = datetime.strptime(entry.name[:-len(".jsonl")], "%Y-%m-%d").date()
                    except ValueError:
                        continue
                    if day < cutoff_date:
                        try:
                            st = entry.stat()
                            os.remove(entry.path)
                            removed_files += 1
                            removed_bytes += st.st_size
                        except Exception:
                            pass

            if removed_files:
                logger.info(
                    f"Retention prune: removed {removed_files} entries "
                    f"({removed_bytes/1024/1024:.1f} MB), retention={retention_days}d"
                )
        except Exception as e:
            logger.warning(f"Retention prune error: {e}")

        await asyncio.sleep(86400)  # once per day
