#!/usr/bin/env bash
#
# scripts/prometheus-clean-stale-cameras.sh — tombstone time-series for cameras
# that no longer exist in cameras:registry.
#
# WHY:
#   Prometheus retains label values until its retention period expires
#   (default 30d in our config). After renaming/deleting cameras, Grafana
#   panels still surface the old labels (e.g. `{camera="front_door"}`
#   or `{camera="ipc_bo"}` from a brief test add) — confusing.
#
# WHAT IT DOES:
#   1. Lists every distinct {camera=...} label value Prometheus knows about
#   2. Compares against cameras:registry — anything in Prometheus that
#      ISN'T in the live registry is stale
#   3. Calls Prometheus's admin tombstone API to mark those series
#      deleted (`/api/v1/admin/tsdb/delete_series`)
#   4. Calls `/api/v1/admin/tsdb/clean_tombstones` to immediately compact
#      and free space
#
# REQUIREMENTS:
#   Prometheus must be started with --web.enable-admin-api (set in
#   docker-compose.yml since Phase H).
#
# USAGE:
#   bash scripts/prometheus-clean-stale-cameras.sh           # interactive — confirms before deleting
#   YES=1 bash scripts/prometheus-clean-stale-cameras.sh     # no prompt

set -euo pipefail

PROM=${PROMETHEUS_URL:-http://localhost:9090}
PROJECT_NAME="${COMPOSE_PROJECT_NAME:-$(basename "$(pwd)" | tr '[:upper:]' '[:lower:]')}"

echo "==> Querying Prometheus for current camera labels..."
PROM_CAMS=$(curl -fsS "$PROM/api/v1/label/camera/values" 2>/dev/null \
  | python3 -c 'import sys,json; print(*json.load(sys.stdin)["data"], sep="\n")')

if [ -z "$PROM_CAMS" ]; then
    echo "Prometheus has no camera labels yet — nothing to clean."
    exit 0
fi
echo "Prometheus knows about these cameras:"
echo "$PROM_CAMS" | sed 's/^/  /'

echo ""
echo "==> Reading live camera registry from Redis..."
LIVE_CAMS=$(docker exec "${PROJECT_NAME}-redis-1" redis-cli HKEYS cameras:registry 2>/dev/null | sort)
echo "Currently registered cameras:"
if [ -z "$LIVE_CAMS" ]; then
    echo "  (none)"
else
    echo "$LIVE_CAMS" | sed 's/^/  /'
fi

STALE=$(comm -23 <(echo "$PROM_CAMS" | sort) <(echo "$LIVE_CAMS" | sort))
if [ -z "$STALE" ]; then
    echo ""
    echo "==> No stale camera labels found. Prometheus matches the registry."
    exit 0
fi

echo ""
echo "==> Stale labels (will be tombstoned):"
echo "$STALE" | sed 's/^/  /'

if [ "${YES:-0}" != "1" ]; then
    echo ""
    read -r -p "Delete these series? (yes/no): " ans
    if [ "$ans" != "yes" ]; then
        echo "Aborted."
        exit 0
    fi
fi

echo ""
echo "==> Issuing tombstones..."
for cam in $STALE; do
    # URL-encoded match[] selector for {camera="<cam>"}
    selector="$(printf '%s' "{camera=\"${cam}\"}" | python3 -c "import sys,urllib.parse; print(urllib.parse.quote(sys.stdin.read().strip(), safe=''))")"
    rc=$(curl -fsS -o /dev/null -w '%{http_code}' \
        -X POST "$PROM/api/v1/admin/tsdb/delete_series?match[]=${selector}")
    if [ "$rc" = "204" ]; then
        echo "  ✓ tombstoned camera=$cam"
    else
        echo "  ✗ HTTP $rc for camera=$cam"
    fi
done

echo ""
echo "==> Compacting tombstones (frees disk + removes from label values)..."
rc=$(curl -fsS -o /dev/null -w '%{http_code}' -X POST "$PROM/api/v1/admin/tsdb/clean_tombstones")
if [ "$rc" = "204" ]; then
    echo "  ✓ tombstones compacted"
else
    echo "  ✗ compaction HTTP $rc"
fi

echo ""
echo "==> Verifying labels are gone..."
sleep 2
curl -fsS "$PROM/api/v1/label/camera/values" 2>/dev/null \
  | python3 -c 'import sys,json; print(*json.load(sys.stdin)["data"], sep="\n")'