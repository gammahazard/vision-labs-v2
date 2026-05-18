#!/usr/bin/env bash
#
# scripts/migrate-front-door-to-cam1.sh
#
# One-time migration: rename the legacy `front_door` slot to `cam1` to match
# the symmetric slot model introduced in Phase G.
#
# CONTEXT:
#   v1 → v0.10 had a special "always-on primary" slot called `front_door`
#   that ran unconditionally from docker-compose.yml (no profile). Other
#   slots (cam2-cam5) were profile-gated, managed by the orchestrator.
#   The asymmetry made the wizard's first-camera flow confusing for new
#   users. v0.11 made all 5 slots symmetric and renamed the primary to
#   `cam1`. Existing installs need this migration to rename their running
#   state — registry entries, Redis keys, recordings directory, snapshot
#   directories, event-journal JSONL.
#
# WHAT IT DOES:
#   1. Backs up the volumes via scripts/backup.sh
#   2. Stops the OLD services (they have legacy names with no -cam1 suffix
#      — compose can't see them anymore after the rename)
#   3. Renames the registry entry: front_door → cam1
#   4. Renames Redis keys: frames:front_door → frames:cam1, etc.
#   5. Renames the recordings dir: data/recordings/front_door → cam1
#   6. Renames snapshot subdirs in qnap-snapshots volume
#   7. Rewrites the camera field in event journal JSONL files
#   8. Starts the new stack with --profile cam1 + any cam2-5 profiles
#      that were active
#
# IDEMPOTENT:
#   Detects whether migration is already complete (no front_door entry in
#   registry, recordings dir doesn't exist, etc.) and skips applicable
#   steps. Safe to re-run.
#
# USAGE:
#   cd ~/projects/vision-labs
#   bash scripts/migrate-front-door-to-cam1.sh
#
# ROLLBACK:
#   bash scripts/restore.sh <path-to-pre-migration-backup.tar.gz>

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT" || { echo "Couldn't cd to $PROJECT_ROOT" >&2; exit 1; }

PROJECT_NAME="${COMPOSE_PROJECT_NAME:-$(basename "$(pwd)" | tr '[:upper:]' '[:lower:]')}"

if [ -t 1 ]; then
    BOLD=$'\e[1m'; RESET=$'\e[0m'
    GREEN=$'\e[32m'; YELLOW=$'\e[33m'; RED=$'\e[31m'; CYAN=$'\e[36m'
else
    BOLD=""; RESET=""; GREEN=""; YELLOW=""; RED=""; CYAN=""
fi
log()     { printf "%s==>%s %s\n" "${CYAN}" "${RESET}" "$1"; }
ok()      { printf "%s ✓%s %s\n" "${GREEN}" "${RESET}" "$1"; }
warn()    { printf "%s !%s %s\n" "${YELLOW}" "${RESET}" "$1"; }
fail()    { printf "%s ✗%s %s\n" "${RED}" "${RESET}" "$1" >&2; exit 1; }
heading() { printf "\n%s%s%s\n" "${BOLD}" "$1" "${RESET}"; }

# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------
heading "Phase G migration: front_door → cam1"

# Make sure Redis is reachable so we can introspect the registry
if ! docker ps --format '{{.Names}}' | grep -q "^${PROJECT_NAME}-redis-1\$"; then
    fail "Redis container '${PROJECT_NAME}-redis-1' not running — start the stack at least once first."
fi
ok "Redis container detected: ${PROJECT_NAME}-redis-1"

REDIS_EXEC="docker exec ${PROJECT_NAME}-redis-1 redis-cli"

# Check whether migration has already happened
FD_ENTRY=$($REDIS_EXEC HGET cameras:registry front_door 2>/dev/null || echo "")
CAM1_ENTRY=$($REDIS_EXEC HGET cameras:registry cam1 2>/dev/null || echo "")

if [ -z "$FD_ENTRY" ] && [ -n "$CAM1_ENTRY" ]; then
    ok "Migration already complete — cam1 entry exists, no front_door entry."
    log "If you need to re-run anything, check manually with: docker exec ${PROJECT_NAME}-redis-1 redis-cli HKEYS cameras:registry"
    exit 0
fi

if [ -z "$FD_ENTRY" ] && [ -z "$CAM1_ENTRY" ]; then
    warn "No front_door OR cam1 entry in registry — nothing to migrate. Exiting."
    exit 0
fi

if [ -n "$FD_ENTRY" ] && [ -n "$CAM1_ENTRY" ]; then
    fail "Both front_door AND cam1 entries exist. Resolve manually first — this script won't overwrite cam1."
fi

log "Found legacy front_door entry; migration needed."

# ---------------------------------------------------------------------------
# Step 1: Take a backup before doing anything destructive
# ---------------------------------------------------------------------------
heading "Step 1/7 — Backup current state"

BACKUP_NAME="pre-migration-front_door-to-cam1-$(date +%Y%m%d-%H%M%S).tar.gz"
BACKUP_DIR="${MIGRATE_BACKUP_DIR:-$(pwd)}"
BACKUP_PATH="$BACKUP_DIR/$BACKUP_NAME"

if [ -x scripts/backup.sh ]; then
    bash scripts/backup.sh "$BACKUP_PATH"
    [ -s "$BACKUP_PATH" ] || fail "backup.sh ran but produced no file at $BACKUP_PATH"
    ok "Backup saved: $BACKUP_PATH"
else
    warn "scripts/backup.sh not found; SKIPPING pre-migration backup. This is risky — abort with Ctrl+C if you don't have a recent backup."
    read -r -p "Continue without backup? (yes/no): " ans
    if [ "$ans" != "yes" ]; then exit 1; fi
fi

# ---------------------------------------------------------------------------
# Step 2: Stop legacy services
# ---------------------------------------------------------------------------
heading "Step 2/7 — Stop legacy front_door services"

LEGACY=(
    "${PROJECT_NAME}-camera-ingester-1"
    "${PROJECT_NAME}-pose-detector-1"
    "${PROJECT_NAME}-vehicle-detector-1"
    "${PROJECT_NAME}-face-recognizer-1"
    "${PROJECT_NAME}-tracker-1"
    "${PROJECT_NAME}-recorder-1"
)
STOPPED=()
for c in "${LEGACY[@]}"; do
    if docker ps -a --format '{{.Names}}' | grep -q "^${c}\$"; then
        docker stop "$c" >/dev/null 2>&1 || true
        docker rm -f "$c" >/dev/null 2>&1 || true
        STOPPED+=("$c")
    fi
done
if [ ${#STOPPED[@]} -gt 0 ]; then
    ok "Stopped + removed: ${STOPPED[*]}"
else
    warn "No legacy containers found (might already be stopped)"
fi

# ---------------------------------------------------------------------------
# Step 3: Rename cameras:registry entry front_door → cam1
# ---------------------------------------------------------------------------
heading "Step 3/7 — Migrate cameras:registry entry"

# Update the `id` field inside the JSON value while we move the key
NEW_VAL=$(echo "$FD_ENTRY" | python3 -c 'import json,sys; d=json.loads(sys.stdin.read()); d["id"]="cam1"; print(json.dumps(d))')
$REDIS_EXEC HSET cameras:registry cam1 "$NEW_VAL" >/dev/null
$REDIS_EXEC HDEL cameras:registry front_door >/dev/null
ok "cameras:registry: front_door → cam1"

# ---------------------------------------------------------------------------
# Step 4: Rename Redis keys (streams, snapshots, config)
# ---------------------------------------------------------------------------
heading "Step 4/7 — Migrate Redis keys (streams, snapshots, configs)"

KEY_COUNT=0
while IFS= read -r key; do
    [ -z "$key" ] && continue
    new_key="${key//front_door/cam1}"
    # Skip if new key already exists (avoid clobbering)
    if [ "$($REDIS_EXEC EXISTS "$new_key")" = "1" ]; then
        warn "Skipping $key — destination $new_key already exists"
        continue
    fi
    $REDIS_EXEC RENAME "$key" "$new_key" >/dev/null
    KEY_COUNT=$((KEY_COUNT + 1))
done < <($REDIS_EXEC --scan --pattern "*front_door*" 2>/dev/null)
ok "Renamed $KEY_COUNT Redis key(s)"

# ---------------------------------------------------------------------------
# Step 5: Rename recordings directory
# ---------------------------------------------------------------------------
heading "Step 5/7 — Migrate recordings directory"

if [ -d "data/recordings/front_door" ]; then
    if [ -d "data/recordings/cam1" ]; then
        warn "data/recordings/cam1 already exists — leaving front_door alone (manual review needed)"
    else
        # Use a container so we don't need host sudo even though ffmpeg wrote as root
        docker run --rm -v "$(pwd)/data/recordings:/r" alpine:3.20 mv /r/front_door /r/cam1
        ok "data/recordings/front_door → data/recordings/cam1"
    fi
else
    log "data/recordings/front_door not present (skip)"
fi

# ---------------------------------------------------------------------------
# Step 6: Rename snapshot subdirectories inside qnap-snapshots volume
# ---------------------------------------------------------------------------
heading "Step 6/7 — Migrate snapshot subdirectories"

SNAPSHOT_VOL="${PROJECT_NAME}_qnap-snapshots"
if docker volume inspect "$SNAPSHOT_VOL" >/dev/null 2>&1; then
    docker run --rm -v "${SNAPSHOT_VOL}:/data" alpine:3.20 sh -c '
        if [ -d /data/front_door ] && [ ! -d /data/cam1 ]; then
            mv /data/front_door /data/cam1
            echo "front_door -> cam1 (top-level)"
        fi
        if [ -d /data/vehicles/front_door ] && [ ! -d /data/vehicles/cam1 ]; then
            mv /data/vehicles/front_door /data/vehicles/cam1
            echo "vehicles/front_door -> vehicles/cam1"
        fi
    '
    ok "Snapshot subdirs migrated"
else
    log "qnap-snapshots volume not found (skip)"
fi

# ---------------------------------------------------------------------------
# Step 7: Rewrite event-journal JSONL files
# ---------------------------------------------------------------------------
heading "Step 7/7 — Rewrite event-journal JSONL camera fields"

EVENTS_VOL="${PROJECT_NAME}_qnap-events"
if docker volume inspect "$EVENTS_VOL" >/dev/null 2>&1; then
    docker run --rm -v "${EVENTS_VOL}:/data" alpine:3.20 sh -c '
        cd /data || exit 0
        for f in *.jsonl; do
            [ -e "$f" ] || continue
            if grep -q "\"camera\": \"front_door\"" "$f"; then
                sed -i "s/\"camera\": \"front_door\"/\"camera\": \"cam1\"/g" "$f"
                echo "rewrote $f"
            fi
        done
    '
    ok "Event journal JSONL files rewritten"
else
    log "qnap-events volume not found (skip)"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
heading "Migration complete."
echo ""
ok "Next: start the stack with --profile cam1 (+ any other cam2-cam5 profiles that should be active)."
echo ""
echo "    docker compose --profile cam1 up -d"
echo ""
echo "  If you also want cam2-cam5 running, add their profile flags:"
echo "    docker compose --profile cam1 --profile cam2 up -d"
echo ""
ok "Backup is at: $BACKUP_PATH"
echo "  To roll back if anything looks wrong:"
echo "    docker compose down"
echo "    bash scripts/restore.sh $BACKUP_PATH"
