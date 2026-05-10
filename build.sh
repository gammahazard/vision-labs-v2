#!/usr/bin/env bash
# build.sh — Sequential Docker Compose build for Windows stability.
#
# Docker on Windows often fails with "rpc error: code = Unavailable"
# when building multiple containers in parallel. This script builds
# each service one at a time with --progress=plain for readable logs.
#
# Usage:
#   ./build.sh          Build all services
#   ./build.sh --up     Build all services then start the stack
#   ./build.sh dashboard tracker   Build only specific services

set -e  # Stop on first failure

SERVICES=(
    camera-ingester
    pose-detector
    vehicle-detector
    tracker
    face-recognizer
    dashboard
)

# If specific services are specified (and not --up), build only those
TARGETS=()
RUN_UP=false

for arg in "$@"; do
    if [ "$arg" == "--up" ]; then
        RUN_UP=true
    else
        TARGETS+=("$arg")
    fi
done

# Default to all services if none specified
if [ ${#TARGETS[@]} -eq 0 ]; then
    TARGETS=("${SERVICES[@]}")
fi

echo "============================================"
echo "  Vision Labs — Sequential Build"
echo "  $(date)"
echo "============================================"
echo ""

# Pull Redis image first (no build needed)
echo "▶ Pulling redis:7-alpine..."
docker compose pull redis
echo "✓ Redis image ready"
echo ""

# Build each service sequentially
FAILED=0
for svc in "${TARGETS[@]}"; do
    echo "──────────────────────────────────────────"
    echo "▶ Building: $svc"
    echo "──────────────────────────────────────────"
    if docker compose build --progress=plain "$svc"; then
        echo "✓ $svc built successfully"
    else
        echo "✗ $svc FAILED"
        FAILED=1
        break
    fi
    echo ""
done

if [ $FAILED -ne 0 ]; then
    echo "============================================"
    echo "  BUILD FAILED — see errors above"
    echo "============================================"
    exit 1
fi

echo "============================================"
echo "  All services built successfully! ✓"
echo "============================================"

if [ "$RUN_UP" = true ]; then
    echo ""
    echo "▶ Starting stack..."
    docker compose up -d
    echo "✓ Stack is running"
    echo ""
    docker compose ps
fi
