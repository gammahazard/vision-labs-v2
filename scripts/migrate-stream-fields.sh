#!/usr/bin/env bash
#
# scripts/migrate-stream-fields.sh — finish the front_door → cam1 migration.
#
# The main migration script (migrate-front-door-to-cam1.sh) renamed the
# Redis STREAM KEYS (events:front_door → events:cam1, etc.) but didn't
# rewrite the FIELD VALUES INSIDE each entry. Many entries store
# `camera_id: "front_door"` and `snapshot_key: "vehicle_snapshot:front_door:..."`
# as inline fields. That causes the dashboard to build snapshot URLs with
# the old camera id, which 404 because the snapshot keys were already
# renamed.
#
# This script rebuilds each affected stream by:
#   1. XRANGE all entries (preserves original IDs)
#   2. For each entry, walk every field/value pair and replace any
#      occurrence of "front_door" inside the VALUE with "cam1"
#   3. Build a fresh stream by XADD'ing each entry with its original ID
#   4. RENAME the new stream over the old one
#
# Idempotent — re-runs are safe (already-migrated entries pass through
# unchanged).
#
# USAGE:
#   bash scripts/migrate-stream-fields.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT" || exit 1

PROJECT_NAME="${COMPOSE_PROJECT_NAME:-$(basename "$(pwd)" | tr '[:upper:]' '[:lower:]')}"

if ! docker ps --format '{{.Names}}' | grep -q "^${PROJECT_NAME}-redis-1\$"; then
    echo "Redis not running" >&2
    exit 1
fi

echo "==> Rewriting stream field values: front_door → cam1"
echo ""

# Streams we want to rewrite. events:cam1 is the most user-visible.
STREAMS=(
    "events:cam1"
    "events:cam2"   # in case there's any cam2 entry that somehow references front_door (unlikely but safe)
    "detections:pose:cam1"
    "detections:vehicle:cam1"
    "identities:cam1"
)

# Use a Python script inside the running dashboard container — it has
# redis-py + the right project context. Streams are read-rebuilt-renamed
# atomically per stream so the dashboard's clients see no torn state.
#
# Note: `docker exec ... python3 - <<'PYEOF'` reliably eats the heredoc
# but the dashboard container's stdin handling isn't always cooperative.
# Write the script to a temp file in the container, then exec it.
PYSCRIPT="/tmp/migrate_stream_fields_$$.py"
docker exec "${PROJECT_NAME}-dashboard-1" sh -c "cat > $PYSCRIPT" <<'PYEOF'
import os
import redis

r = redis.Redis(host="redis", port=6379, decode_responses=True)

STREAMS = [
    "events:cam1",
    "events:cam2",
    "detections:pose:cam1",
    "detections:vehicle:cam1",
    "identities:cam1",
]

def rebuild(stream: str) -> tuple[int, int]:
    """Walk stream, rewrite any field value containing 'front_door' → 'cam1'.

    Returns (entries_processed, entries_rewritten). Streams without any
    front_door references are still rebuilt (no-op data-wise) — that's fine.
    """
    try:
        entries = r.xrange(stream)
    except redis.ResponseError:
        return 0, 0

    if not entries:
        return 0, 0

    temp = f"{stream}__migrate_tmp"
    r.delete(temp)

    rewritten = 0
    for entry_id, fields in entries:
        new_fields = {}
        changed = False
        for k, v in fields.items():
            if isinstance(v, str) and "front_door" in v:
                new_fields[k] = v.replace("front_door", "cam1")
                changed = True
            else:
                new_fields[k] = v
        if changed:
            rewritten += 1
        # XADD with explicit id — preserves the original time-ordering
        r.xadd(temp, new_fields, id=entry_id)

    # Atomic swap
    r.rename(temp, stream)
    return len(entries), rewritten

for s in STREAMS:
    total, rewritten = rebuild(s)
    if total:
        print(f"  {s}: {total} entries, {rewritten} rewritten")
    else:
        print(f"  {s}: not present or empty (skipped)")

print()
print("Done.")
PYEOF

# Run the script we just wrote, then clean up
docker exec "${PROJECT_NAME}-dashboard-1" python3 "$PYSCRIPT"
docker exec "${PROJECT_NAME}-dashboard-1" rm -f "$PYSCRIPT"