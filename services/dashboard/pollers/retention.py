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
    1a. `/data/snapshots/*.jpg` — legacy flat snapshots (pre-fan-out), by mtime
    1b. `/data/snapshots/{camera_id}/*.jpg` — per-camera snapshots, by mtime
    2a. `/data/snapshots/vehicles/YYYY-MM-DD/` — legacy vehicle subfolders, by name
    2b. `/data/snapshots/vehicles/{camera_id}/YYYY-MM-DD/` — per-camera, by name
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
    """Daily prune of /data/snapshots and /data/events older than configured retention."""
    retention_days = int(os.getenv("SNAPSHOT_RETENTION_DAYS", "4"))
    # Clips are bigger than person snapshots — separate (shorter) retention.
    clip_retention_days = int(os.getenv("CLIP_RETENTION_DAYS", "3"))
    if retention_days <= 0:
        logger.info("Local retention disabled (SNAPSHOT_RETENTION_DAYS=0)")
        return

    SNAPSHOT_DIR = os.environ.get("SNAPSHOT_DIR", "/data/snapshots")
    EVENT_DIR = os.environ.get("EVENT_JOURNAL_DIR", "/data/events")
    CLIPS_DIR = os.path.join(SNAPSHOT_DIR, "clips")

    logger.info(
        f"Local retention enabled — snapshots/events {retention_days}d, "
        f"clips {clip_retention_days}d (snapshots={SNAPSHOT_DIR}, "
        f"clips={CLIPS_DIR}, events={EVENT_DIR})"
    )

    await asyncio.sleep(60)  # let the rest of startup finish

    while True:
        try:
            cutoff_ts = time.time() - (retention_days * 86400)
            cutoff_date = (datetime.now() - timedelta(days=retention_days)).date()
            clip_cutoff_ts = time.time() - (clip_retention_days * 86400)
            removed_files = 0
            removed_bytes = 0

            # Prune clips (AI assistant + /clip Telegram outputs)
            if os.path.isdir(CLIPS_DIR):
                for entry in os.scandir(CLIPS_DIR):
                    if not entry.is_file():
                        continue
                    try:
                        st = entry.stat()
                        if st.st_mtime < clip_cutoff_ts:
                            os.remove(entry.path)
                            removed_files += 1
                            removed_bytes += st.st_size
                    except Exception:
                        pass

            # 1a. Legacy flat /data/snapshots/*.jpg (pre-fan-out snapshots)
            # 1b. Per-camera /data/snapshots/{camera_id}/*.jpg (current layout)
            if os.path.isdir(SNAPSHOT_DIR):
                # Legacy root
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
                # Per-camera subdirs (skip reserved subdirs)
                RESERVED_SUBDIRS = {"vehicles", "clips"}
                for cam_entry in os.scandir(SNAPSHOT_DIR):
                    if not cam_entry.is_dir() or cam_entry.name in RESERVED_SUBDIRS:
                        continue
                    try:
                        for entry in os.scandir(cam_entry.path):
                            if entry.is_file() and entry.name.endswith(".jpg"):
                                try:
                                    st = entry.stat()
                                    if st.st_mtime < cutoff_ts:
                                        os.remove(entry.path)
                                        removed_files += 1
                                        removed_bytes += st.st_size
                                except Exception:
                                    pass
                    except Exception:
                        pass

            # 2a. Legacy vehicle snapshots: /data/snapshots/vehicles/YYYY-MM-DD/
            # 2b. Per-camera: /data/snapshots/vehicles/{camera_id}/YYYY-MM-DD/
            vehicles_dir = os.path.join(SNAPSHOT_DIR, "vehicles")
            if os.path.isdir(vehicles_dir):
                for entry in os.scandir(vehicles_dir):
                    if not entry.is_dir():
                        continue
                    # Try legacy YYYY-MM-DD directly under vehicles/
                    try:
                        day = datetime.strptime(entry.name, "%Y-%m-%d").date()
                        if day < cutoff_date:
                            try:
                                shutil.rmtree(entry.path)
                                removed_files += 1
                            except Exception:
                                pass
                        continue
                    except ValueError:
                        pass
                    # Otherwise it's a per-camera dir; walk its date subdirs
                    try:
                        for day_entry in os.scandir(entry.path):
                            if not day_entry.is_dir():
                                continue
                            try:
                                day = datetime.strptime(day_entry.name, "%Y-%m-%d").date()
                            except ValueError:
                                continue
                            if day < cutoff_date:
                                try:
                                    shutil.rmtree(day_entry.path)
                                    removed_files += 1
                                except Exception:
                                    pass
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
