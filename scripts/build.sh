#!/usr/bin/env bash
#
# scripts/build.sh — build the Vision Labs stack from scratch.
#
# WHY THIS EXISTS:
#   The detector Dockerfiles reference `FROM vision-labs-base:cuda12.8`,
#   which `docker compose build` won't build automatically (it doesn't
#   crawl Dockerfiles for unknown FROM tags). So the base must be built
#   first, then compose builds everything that depends on it.
#
# USAGE:
#   bash scripts/build.sh                # build base + every service
#   bash scripts/build.sh --no-cache     # force fresh build (slow; ~30 min)
#
# WHAT YOU GET:
#   - vision-labs-base:cuda12.8       (shared CUDA + Python + system deps)
#   - vision-labs-pose-detector:latest
#   - vision-labs-vehicle-detector:latest
#   - vision-labs-face-recognizer:latest
#   - vision-labs-dashboard:latest
#   - vision-labs-camera-ingester:latest
#   - vision-labs-recorder:latest
#   - vision-labs-tracker:latest
#   - vision-labs-orchestrator:latest
#
# After this completes, `docker compose up -d` starts the stack.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ARGS=("$@")

echo "==> Building shared base image (vision-labs-base:cuda12.8)..."
docker build "${ARGS[@]}" -t vision-labs-base:cuda12.8 services/base

echo "==> Building all service images via docker compose..."
docker compose build "${ARGS[@]}"

echo ""
echo "✓ Build complete."
echo ""
echo "Next steps:"
echo "  docker compose up -d                # start the stack"
echo "  docker compose logs -f dashboard    # watch logs"
