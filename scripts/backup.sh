#!/usr/bin/env bash
#
# scripts/backup.sh — snapshot the data-bearing Docker volumes.
#
# WHAT IT BACKS UP:
#   face-data       — faces.db + enrolled face photos (most important —
#                     all your enrolled identities + unknowns are here)
#   auth-data       — admin DB (sessions, passwords), AI chat history,
#                     setup wizard state
#   redis-data      — Redis AOF (camera registry, events stream, configs)
#   qnap-snapshots  — person + vehicle snapshot JPEGs
#   qnap-events     — daily event-journal JSONL files
#   qnap-telegram   — Telegram message + media archive
#
# WHAT IT DOES NOT BACK UP:
#   - DVR recordings (./data/recordings/) — these are already a host bind
#     mount, so they survive everything except disk wipe. Copy that
#     directory separately if you want to archive recordings.
#   - YOLO / InsightFace / Ollama model caches — re-downloadable on next
#     run, not worth GBs in backups.
#   - Prometheus + Grafana state — metrics history, low value.
#
# USAGE:
#   bash scripts/backup.sh                            # default filename
#   bash scripts/backup.sh /path/to/my-backup.tar.gz  # custom filename
#
# RESTORE:
#   bash scripts/restore.sh <path-to-tarball>
#
# OPERATIONAL NOTES:
#   - Volumes are mounted read-only during backup; the stack keeps running.
#   - For a perfectly consistent SQLite snapshot you could `docker compose stop`
#     first, but for normal use a live read-only tar is fine — SQLite WAL
#     mode means writes are crash-safe.
#   - The tarball lands in $(pwd) by default, NOT inside the repo. Move it
#     to OneDrive / a USB / wherever you keep backups.

set -euo pipefail

# Project name = the prefix on the volume names. Compose derives this from
# either COMPOSE_PROJECT_NAME or the directory containing docker-compose.yml
# (lowercased, but otherwise unchanged).
PROJECT_NAME="${COMPOSE_PROJECT_NAME:-$(basename "$(pwd)" | tr '[:upper:]' '[:lower:]')}"

# Volumes to back up. Note these are the SHORT names from docker-compose.yml;
# docker actually stores them as "${PROJECT_NAME}_${name}".
VOLUMES=(
    "face-data"
    "auth-data"
    "redis-data"
    "qnap-snapshots"
    "qnap-events"
    "qnap-telegram"
)

# Default output: timestamped, in current dir.
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
DEFAULT_OUTPUT="vl-backup-${TIMESTAMP}.tar.gz"
OUTPUT="${1:-$DEFAULT_OUTPUT}"

# Resolve to an absolute path so we can mount its parent directory into
# the helper container regardless of where the user invokes from.
case "$OUTPUT" in
    /*) ABS_OUTPUT="$OUTPUT" ;;
    *)  ABS_OUTPUT="$(pwd)/$OUTPUT" ;;
esac
OUTPUT_DIR="$(dirname "$ABS_OUTPUT")"
OUTPUT_FILE="$(basename "$ABS_OUTPUT")"

if [ ! -d "$OUTPUT_DIR" ]; then
    echo "Output directory does not exist: $OUTPUT_DIR" >&2
    exit 1
fi

echo "==> Backing up Vision Labs volumes for project: $PROJECT_NAME"
echo "    Output: $ABS_OUTPUT"
echo

# Sanity-check each volume exists before mounting. A missing volume means
# either compose hasn't been run yet, or COMPOSE_PROJECT_NAME is wrong.
MISSING=()
for v in "${VOLUMES[@]}"; do
    full="${PROJECT_NAME}_${v}"
    if ! docker volume inspect "$full" >/dev/null 2>&1; then
        MISSING+=("$full")
    fi
done

if [ ${#MISSING[@]} -gt 0 ]; then
    echo "ERROR: These volumes don't exist (yet):" >&2
    for v in "${MISSING[@]}"; do echo "  - $v" >&2; done
    echo >&2
    echo "If your project directory isn't called 'vision-labs', set COMPOSE_PROJECT_NAME:" >&2
    echo "    COMPOSE_PROJECT_NAME=<the-right-name> bash scripts/backup.sh" >&2
    exit 1
fi

# Build the -v flags for the helper container. Each volume mounts read-only
# at /volumes/<short-name>/, so the resulting tar has predictable paths.
MOUNT_FLAGS=()
for v in "${VOLUMES[@]}"; do
    MOUNT_FLAGS+=("-v" "${PROJECT_NAME}_${v}:/volumes/${v}:ro")
done

# Run tar inside a throwaway alpine container. Mount the output directory
# so the result lands on the host.
echo "==> Creating tarball (this is fast — volumes total a few hundred MB)..."
docker run --rm \
    "${MOUNT_FLAGS[@]}" \
    -v "$OUTPUT_DIR:/backup" \
    alpine:3.20 \
    sh -c "cd /volumes && tar czf /backup/${OUTPUT_FILE} ."

# Sanity-check the resulting file
if [ ! -s "$ABS_OUTPUT" ]; then
    echo "ERROR: Backup file is empty or missing: $ABS_OUTPUT" >&2
    exit 1
fi

SIZE_HUMAN=$(du -h "$ABS_OUTPUT" | cut -f1)
echo
echo "✓ Backup complete."
echo "  File:   $ABS_OUTPUT"
echo "  Size:   $SIZE_HUMAN"
echo
echo "To restore (DESTRUCTIVE — overwrites current state):"
echo "    bash scripts/restore.sh $ABS_OUTPUT"
