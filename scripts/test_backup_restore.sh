#!/usr/bin/env bash
#
# scripts/test_backup_restore.sh — non-destructive smoke test for backup.sh.
#
# WHY:
#   Backup files are theater unless someone has actually restored from them.
#   This script verifies a fresh backup is:
#     - Non-empty
#     - Properly structured (auth.db + faces.db are inside the tarball)
#     - Not corrupted (each .db file has the SQLite magic header)
#
#   It does NOT touch the running stack and does NOT need extra permissions
#   beyond running docker (same as backup.sh itself). The extracted tarball
#   files are owned by root inside the helper container, so we deliberately
#   avoid trying to query them — just check file presence + magic header.
#
# USAGE:
#   bash scripts/test_backup_restore.sh
#
# EXIT CODES:
#   0  → backup looks restorable
#   1  → backup failed or tarball is unusable
#
# COULD RUN AS A CRON:
#   0 4 * * 1 cd /home/.../vision-labs && bash scripts/test_backup_restore.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TMP_DIR="$(mktemp -d -t vl-backup-smoke-XXXXXX)"
TARBALL="$TMP_DIR/test-backup.tar.gz"

cleanup() {
    if [ -n "${TMP_DIR:-}" ] && [ -d "$TMP_DIR" ]; then
        # Run the rm inside docker too, since extracted files may be root-owned
        docker run --rm -v "$TMP_DIR:/tmp/x" alpine:3.20 sh -c "rm -rf /tmp/x/*" 2>/dev/null || true
        rm -rf "$TMP_DIR" 2>/dev/null || true
    fi
}
trap cleanup EXIT

cd "$REPO_ROOT"

# ----------------------------------------------------------------------------
# Step 1 — produce a fresh backup
# ----------------------------------------------------------------------------
echo "==> Creating a fresh backup at $TARBALL"
if ! bash scripts/backup.sh "$TARBALL" >/dev/null 2>&1; then
    echo "FAIL: scripts/backup.sh exited non-zero" >&2
    bash scripts/backup.sh "$TARBALL" 2>&1 | tail -10 >&2
    exit 1
fi

if [ ! -s "$TARBALL" ]; then
    echo "FAIL: tarball missing or empty: $TARBALL" >&2
    exit 1
fi

SIZE_BYTES=$(stat -c%s "$TARBALL" 2>/dev/null || wc -c < "$TARBALL")
echo "    backup size: $SIZE_BYTES bytes ($(numfmt --to=iec "$SIZE_BYTES" 2>/dev/null || echo unknown))"

if [ "$SIZE_BYTES" -lt 4096 ]; then
    echo "FAIL: tarball <4 KB ($SIZE_BYTES bytes) — almost certainly empty volumes" >&2
    exit 1
fi

# ----------------------------------------------------------------------------
# Step 2 — verify expected files are present in the tarball
# ----------------------------------------------------------------------------
echo "==> Checking tarball contents"
LIST=$(tar tzf "$TARBALL" 2>/dev/null)
if [ -z "$LIST" ]; then
    echo "FAIL: tarball appears unreadable (tar tzf returned nothing)" >&2
    exit 1
fi

REQUIRED=("auth-data/auth.db" "face-data/faces.db" "redis-data/")
MISSING=()
# Use bash native substring match — avoids `grep -q` which triggers SIGPIPE
# under `set -o pipefail` and silently fails this loop.
for entry in "${REQUIRED[@]}"; do
    if [[ "$LIST" != *"$entry"* ]]; then
        MISSING+=("$entry")
    fi
done

if [ ${#MISSING[@]} -gt 0 ]; then
    echo "FAIL: tarball missing required entries:" >&2
    for m in "${MISSING[@]}"; do echo "  - $m" >&2; done
    echo "Full tarball contents (head 30):" >&2
    echo "$LIST" | head -30 >&2 || true
    exit 1
fi
echo "    auth.db + faces.db + redis-data all present"

# ----------------------------------------------------------------------------
# Step 3 — verify each SQLite DB has the magic header by streaming it from
# the tarball (no extraction needed, so no permission issues)
# ----------------------------------------------------------------------------
echo "==> Checking SQLite magic headers"
# SQLite files start with the literal bytes "SQLite format 3\0".
# `set -o pipefail` + `head -c 16` causes SIGPIPE-induced failure in upstream
# tar — disable pipefail just for this block to avoid false-fail.
set +o pipefail

for db_path in "auth-data/auth.db" "face-data/faces.db"; do
    HEADER=$(tar xzOf "$TARBALL" "./$db_path" 2>/dev/null | head -c 16 | tr -d '\0')
    case "$HEADER" in
        SQLite\ format\ 3*)
            size=$(tar tzvf "$TARBALL" 2>/dev/null | awk -v p="./$db_path" '$0 ~ p {print $3; exit}')
            echo "    $db_path: OK (size: $size bytes)"
            ;;
        *)
            set -o pipefail
            echo "FAIL: $db_path is not a valid SQLite database (got header: $HEADER)" >&2
            exit 1
            ;;
    esac
done
set -o pipefail

# ----------------------------------------------------------------------------
# Step 4 — Redis state present?
# ----------------------------------------------------------------------------
echo "==> Checking Redis state"
HAS_AOF=$(echo "$LIST" | grep -c "redis-data.*\.aof" || true)
HAS_DUMP=$(echo "$LIST" | grep -c "redis-data.*dump\.rdb" || true)
HAS_BASE=$(echo "$LIST" | grep -c "redis-data.*\.base\.rdb" || true)
if [ "$HAS_AOF" -eq 0 ] && [ "$HAS_DUMP" -eq 0 ] && [ "$HAS_BASE" -eq 0 ]; then
    echo "WARN: redis-data has no AOF/RDB — restore would yield empty Redis"
else
    echo "    redis-data OK (aof=$HAS_AOF, dump.rdb=$HAS_DUMP, base.rdb=$HAS_BASE)"
fi

echo ""
echo "✅ Backup round-trip smoke test PASSED"
echo "    tarball: $SIZE_BYTES bytes ($(numfmt --to=iec "$SIZE_BYTES" 2>/dev/null || echo ?))"
echo "    location (NOT cleaned up — verify the path is correct, then delete): N/A (auto-cleaned)"
echo ""
echo "Note: this verifies the tarball is restorable in shape. To exercise the"
echo "full restore.sh flow safely, spin up a separate compose project with"
echo "COMPOSE_PROJECT_NAME=vl-test and run restore.sh against it."
