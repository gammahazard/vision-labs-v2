#!/usr/bin/env bash
#
# scripts/restore.sh — restore a backup made by scripts/backup.sh.
#
# WHAT IT DOES:
#   1. Verifies the backup tarball exists and is non-empty
#   2. Verifies all expected Docker volumes exist (creates them if not)
#   3. Stops the running stack (so SQLite/Redis aren't writing during restore)
#   4. Wipes + restores each volume from the tarball
#   5. Restarts the stack
#
# WHAT IT REPLACES (destructively):
#   - All enrolled faces + unknowns (faces.db)
#   - Admin DB (sessions get invalidated; you may need to log in again)
#   - AI chat history
#   - Setup wizard state
#   - Redis (camera registry, events stream, AI config)
#   - Snapshots, event journal, Telegram media
#
# WHAT IT DOES NOT TOUCH:
#   - DVR recordings (./data/recordings/) — those are a host bind mount
#   - Model caches (YOLO, InsightFace, Ollama)
#   - Docker images themselves
#   - Your .env file
#
# USAGE:
#   bash scripts/restore.sh path/to/vl-backup-20260518-153000.tar.gz
#
# CAUTION:
#   This OVERWRITES current data. If you have unknowns/enrollments in the
#   live state that aren't in the backup, they're gone. Consider running
#   backup.sh BEFORE restore.sh to snapshot current state as well.

set -euo pipefail

if [ $# -ne 1 ]; then
    echo "Usage: bash scripts/restore.sh <path-to-backup.tar.gz>" >&2
    exit 1
fi

INPUT="$1"

# Resolve to absolute path
case "$INPUT" in
    /*) ABS_INPUT="$INPUT" ;;
    *)  ABS_INPUT="$(pwd)/$INPUT" ;;
esac

if [ ! -s "$ABS_INPUT" ]; then
    echo "ERROR: Backup file not found or empty: $ABS_INPUT" >&2
    exit 1
fi

INPUT_DIR="$(dirname "$ABS_INPUT")"
INPUT_FILE="$(basename "$ABS_INPUT")"

PROJECT_NAME="${COMPOSE_PROJECT_NAME:-$(basename "$(pwd)" | tr '[:upper:]' '[:lower:]')}"

VOLUMES=(
    "face-data"
    "auth-data"
    "redis-data"
    "qnap-snapshots"
    "qnap-events"
    "qnap-telegram"
)

echo "==> Restore plan"
echo "    Backup:  $ABS_INPUT"
echo "    Project: $PROJECT_NAME"
echo "    Volumes: ${VOLUMES[*]}"
echo
echo "This will OVERWRITE all current state in those volumes. Including:"
echo "  - Enrolled faces + unknowns"
echo "  - Admin DB (you may need to log back in)"
echo "  - Camera registry + event stream"
echo "  - Snapshots + event journal"
echo
echo "DVR recordings (./data/recordings/) are NOT touched."
echo
echo "Press Enter to proceed, or Ctrl+C to abort."
read -r _

# Peek inside the tarball to confirm it has the expected layout
echo "==> Validating tarball contents..."
EXPECTED_DIRS=""
for v in "${VOLUMES[@]}"; do EXPECTED_DIRS="$EXPECTED_DIRS ./$v/"; done
ACTUAL=$(tar tzf "$ABS_INPUT" 2>/dev/null | awk -F/ '{print $1"/"$2}' | sort -u | head -20)

for v in "${VOLUMES[@]}"; do
    if ! echo "$ACTUAL" | grep -q "^\./${v}\$\|^\./${v}/"; then
        echo "WARNING: backup doesn't contain './$v/' — volume will be left empty after restore" >&2
    fi
done

# Stop the stack so we're not racing with running processes
if docker compose ps -q 2>/dev/null | grep -q .; then
    echo "==> Stopping the stack..."
    docker compose down
fi

# Make sure every volume exists. `docker compose up --no-start` would do
# this too, but we want each volume specifically (some may be from
# profile-gated services not in the default compose).
echo "==> Ensuring target volumes exist..."
for v in "${VOLUMES[@]}"; do
    full="${PROJECT_NAME}_${v}"
    if ! docker volume inspect "$full" >/dev/null 2>&1; then
        docker volume create "$full" >/dev/null
        echo "    created $full"
    fi
done

# Wipe + restore each volume. We wipe BEFORE extracting so the restore
# doesn't leave around files that aren't in the backup.
MOUNT_FLAGS=()
for v in "${VOLUMES[@]}"; do
    MOUNT_FLAGS+=("-v" "${PROJECT_NAME}_${v}:/volumes/${v}")
done

echo "==> Wiping target volumes..."
docker run --rm \
    "${MOUNT_FLAGS[@]}" \
    alpine:3.20 \
    sh -c 'for d in /volumes/*; do find "$d" -mindepth 1 -delete; done'

echo "==> Extracting backup..."
docker run --rm \
    "${MOUNT_FLAGS[@]}" \
    -v "$INPUT_DIR:/backup:ro" \
    alpine:3.20 \
    sh -c "cd /volumes && tar xzf /backup/${INPUT_FILE}"

echo "==> Restarting the stack..."
docker compose up -d

# Wait briefly for redis to come back so we can sanity-check the camera registry
sleep 4
CAM_COUNT=$(docker exec "${PROJECT_NAME}-redis-1" redis-cli HLEN cameras:registry 2>/dev/null || echo "?")
echo
echo "✓ Restore complete."
echo "  Camera registry has $CAM_COUNT entries"
echo "  Open the dashboard to verify your unknowns/enrollments are present."
