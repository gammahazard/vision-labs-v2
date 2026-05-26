#!/usr/bin/env bash
#
# scripts/install-linux.sh — Vision Labs installer for Ubuntu/Debian.
#
# WHAT IT DOES:
#   1. Verifies an NVIDIA GPU is present (this stack is CUDA-only)
#   2. Installs Docker Engine + Docker Compose if missing
#   3. Installs NVIDIA Container Toolkit if missing
#   4. Adds the current user to the docker group
#   5. Verifies GPU passthrough works in a container
#   6. Copies .env.example -> .env if needed (prompts you to edit it after)
#   7. Pulls pre-built images from ghcr.io/gammahazard/vision-labs/* (default)
#      OR builds locally with --build / BUILD_FROM_SOURCE=1
#   8. Starts the stack with `docker compose up -d`
#   9. Tells you where the dashboard is
#
# USAGE:
#   Inside a cloned vision-labs checkout:
#     bash scripts/install-linux.sh                     # pulls from GHCR (fast)
#     bash scripts/install-linux.sh --build             # builds locally (~15 min)
#     IMAGE_TAG=v0.1.0 bash scripts/install-linux.sh    # pin a specific release
#
#   Idempotent — safe to re-run. Skips steps that are already done.
#
# WHAT THIS SCRIPT DOES NOT DO:
#   - Modify your /etc/sudoers, kernel boot params, or systemd config
#     beyond Docker's normal setup
#   - Touch your firewall (no ufw rule changes)
#   - Install a desktop environment or change display settings
#
# TESTED ON:
#   Ubuntu 22.04, Ubuntu 24.04, Debian 12 (with NVIDIA driver pre-installed)
#
# REQUIREMENTS:
#   - sudo privileges (most steps need root)
#   - NVIDIA GPU + recent NVIDIA driver already installed
#     (this script installs the *container toolkit*, NOT the driver)
#   - 25+ GB free disk space (CUDA + PyTorch images are large)
#   - 16+ GB RAM recommended
#   - Internet connection (apt + docker registry pulls)

set -euo pipefail

# Install mode: default to pulling pre-built images from GHCR. Users who've
# forked or edited the code can pass --build (or set BUILD_FROM_SOURCE=1) to
# force the local-build path instead.
INSTALL_MODE="pull"
for arg in "$@"; do
    case "$arg" in
        --build|--from-source) INSTALL_MODE="build" ;;
        --pull|--from-registry) INSTALL_MODE="pull" ;;
        -h|--help)
            cat <<'EOF'
Vision Labs Linux installer.

Usage:
    bash scripts/install-linux.sh             # pull pre-built images from GHCR (default, ~3-5 min)
    bash scripts/install-linux.sh --build     # build locally (~10-15 min — use if you've modified code)

Env vars:
    IMAGE_TAG=v0.1.0    Pin a specific release tag (default: latest)
    BUILD_FROM_SOURCE=1 Same as --build
EOF
            exit 0
            ;;
    esac
done
if [ "${BUILD_FROM_SOURCE:-0}" = "1" ]; then
    INSTALL_MODE="build"
fi

# ANSI colour helpers — only when stdout is a TTY
if [ -t 1 ]; then
    BOLD=$'\e[1m'; RESET=$'\e[0m'
    GREEN=$'\e[32m'; YELLOW=$'\e[33m'; RED=$'\e[31m'; CYAN=$'\e[36m'
else
    BOLD=""; RESET=""; GREEN=""; YELLOW=""; RED=""; CYAN=""
fi

log()   { printf "%s==>%s %s\n" "${CYAN}" "${RESET}" "$1"; }
ok()    { printf "%s ✓%s %s\n" "${GREEN}" "${RESET}" "$1"; }
warn()  { printf "%s ⚠%s %s\n" "${YELLOW}" "${RESET}" "$1"; }
fail()  { printf "%s ✗%s %s\n" "${RED}" "${RESET}" "$1" >&2; exit 1; }
heading() { printf "\n%s%s%s\n" "${BOLD}" "$1" "${RESET}"; }

# Run a command with sudo, but only prompt for password once.
# We pre-warm sudo so the user only sees the prompt at the start.
need_sudo() {
    if [ "$(id -u)" -ne 0 ]; then
        log "This script needs sudo for some steps. You may be prompted now."
        sudo -v || fail "sudo is required."
        # Keep sudo alive in background while we work
        ( while true; do sudo -n true; sleep 60; kill -0 "$$" 2>/dev/null || exit; done ) &
        SUDO_KEEPALIVE_PID=$!
        trap 'kill $SUDO_KEEPALIVE_PID 2>/dev/null || true' EXIT
    fi
}

# ---------------------------------------------------------------------------
# Step 1: Sanity — what OS, what GPU?
# ---------------------------------------------------------------------------
heading "Vision Labs Linux installer"

if [ -f /etc/os-release ]; then
    . /etc/os-release
    log "Detected: ${PRETTY_NAME:-${ID:-unknown}}"
    case "${ID:-}" in
        ubuntu|debian) ;;
        *) warn "Tested on Ubuntu and Debian; ${ID:-unknown} may need adjustments." ;;
    esac
else
    warn "Couldn't read /etc/os-release; proceeding with best-effort defaults."
fi

if ! command -v nvidia-smi >/dev/null 2>&1; then
    fail "nvidia-smi not found. Install the NVIDIA driver for your GPU first, then re-run.
        Ubuntu: sudo apt install nvidia-driver-550-server  (or the version your card needs)
        Reboot afterwards, then come back to this script."
fi

if ! nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | grep -q .; then
    fail "nvidia-smi is installed but returns no GPUs. Driver may be broken — try rebooting."
fi
# Read the first line into a variable without piping to `head`. Piping to
# `head -1` would close the pipe after one line and SIGPIPE nvidia-smi
# (especially on multi-GPU hosts), which under `set -o pipefail` makes
# the whole pipeline non-zero and `set -e` exits silently. Using
# process-substitution + `read` avoids the broken pipe.
read -r GPU_NAME < <(nvidia-smi --query-gpu=name --format=csv,noheader) || true
ok "GPU detected: ${GPU_NAME:-unknown}"

need_sudo

# ---------------------------------------------------------------------------
# Step 2: Docker Engine
# ---------------------------------------------------------------------------
heading "Step 1/5 — Docker Engine"

if command -v docker >/dev/null 2>&1 && docker --version >/dev/null 2>&1; then
    ok "Docker is already installed ($(docker --version))"
else
    log "Installing Docker via get.docker.com..."
    curl -fsSL https://get.docker.com | sudo sh
    ok "Docker installed"
fi

if ! getent group docker >/dev/null 2>&1; then
    sudo groupadd docker
fi

if ! id -nG "$USER" 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
    log "Adding $USER to the docker group (avoids needing sudo for every command)..."
    sudo usermod -aG docker "$USER"
    warn "You'll need to log out + back in (or run 'newgrp docker') for this to take effect."
    NEW_DOCKER_GROUP=1
fi

# ---------------------------------------------------------------------------
# Step 3: NVIDIA Container Toolkit
# ---------------------------------------------------------------------------
heading "Step 2/5 — NVIDIA Container Toolkit"

if dpkg -s nvidia-container-toolkit >/dev/null 2>&1; then
    ok "nvidia-container-toolkit already installed"
else
    log "Installing nvidia-container-toolkit..."
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
        | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
        | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
        | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
    sudo apt-get update -qq
    sudo apt-get install -y nvidia-container-toolkit

    log "Configuring Docker to use the NVIDIA runtime..."
    sudo nvidia-ctk runtime configure --runtime=docker
    sudo systemctl restart docker
    ok "NVIDIA Container Toolkit configured"
fi

# ---------------------------------------------------------------------------
# Step 4: Verify GPU passthrough
# ---------------------------------------------------------------------------
heading "Step 3/5 — GPU passthrough check"

# Use sudo for this test since the docker-group change won't apply until logout
if sudo docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi --query-gpu=name --format=csv,noheader >/dev/null 2>&1; then
    ok "GPU is visible inside Docker containers"
else
    fail "Couldn't see GPU from a container. Check 'sudo docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi' for the actual error."
fi

# ---------------------------------------------------------------------------
# Step 5: .env setup
# ---------------------------------------------------------------------------
heading "Step 4/5 — Configuration (.env)"

# Find the project root (the dir containing docker-compose.yml above this script)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT" || fail "Couldn't cd to project root: $PROJECT_ROOT"

if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        cp .env.example .env
        ok "Created .env from .env.example"
        warn "You'll want to edit .env later — at minimum set CAMERA_IP, CAMERA_USER, CAMERA_PASSWORD if you have a camera ready."
        warn "(But you can also use the wizard at http://localhost:8080 to add cameras after the stack starts.)"
    else
        fail ".env.example not found — are you running this from a vision-labs checkout?"
    fi
else
    ok ".env already exists — leaving it untouched"
fi

# Generate a Redis password if one isn't already set. The dashboard, every
# detector, and the tracker all read REDIS_PASSWORD via make_redis_client;
# the redis container only requires AUTH when this is non-empty. We seed it
# once at install so nobody has to think about it — it's never typed by a
# human, so a 32-byte hex value is fine.
if ! grep -qE "^REDIS_PASSWORD=[A-Za-z0-9]" .env 2>/dev/null; then
    REDIS_PW="$(openssl rand -hex 32 2>/dev/null || head -c 32 /dev/urandom | base64 | tr -d '/+=')"
    if [ -n "$REDIS_PW" ]; then
        # Remove any blank/commented REDIS_PASSWORD line, then append the real one.
        sed -i '/^[[:space:]]*#*[[:space:]]*REDIS_PASSWORD=/d' .env
        printf '\n# Redis AUTH — auto-generated at install. Required by every service.\nREDIS_PASSWORD=%s\n' "$REDIS_PW" >> .env
        ok "Generated REDIS_PASSWORD (32 bytes, hex) and added to .env"
    else
        warn "Could not generate REDIS_PASSWORD — openssl + /dev/urandom both unavailable. Add a value manually before starting the stack."
    fi
else
    ok "REDIS_PASSWORD already set — leaving it alone"
fi

# Generate a Grafana admin password if one isn't set. The compose file reads
# GRAFANA_ADMIN_PASSWORD from .env; we never hardcode it because the repo is
# public. Anonymous Viewer (for the embedded panels) is unaffected — this
# only protects the admin/edit login.
if ! grep -qE "^GRAFANA_ADMIN_PASSWORD=[A-Za-z0-9]" .env 2>/dev/null; then
    GRAFANA_PW="$(openssl rand -hex 24 2>/dev/null || head -c 24 /dev/urandom | base64 | tr -d '/+=')"
    if [ -n "$GRAFANA_PW" ]; then
        sed -i '/^[[:space:]]*#*[[:space:]]*GRAFANA_ADMIN_PASSWORD=/d' .env
        printf '\n# Grafana admin password — auto-generated at install. Never commit a value.\nGRAFANA_ADMIN_PASSWORD=%s\n' "$GRAFANA_PW" >> .env
        ok "Generated GRAFANA_ADMIN_PASSWORD and added to .env"
    else
        warn "Could not generate GRAFANA_ADMIN_PASSWORD — set one manually before starting the stack."
    fi
else
    ok "GRAFANA_ADMIN_PASSWORD already set — leaving it alone"
fi

# ---------------------------------------------------------------------------
# Step 6: Pull or build, then run
# ---------------------------------------------------------------------------
if [ "$INSTALL_MODE" = "build" ]; then
    heading "Step 5/5 — Build + start the stack (local build)"
else
    heading "Step 5/5 — Pull pre-built images + start the stack"
fi

DOCKER_CMD="docker"
if [ "${NEW_DOCKER_GROUP:-0}" = "1" ]; then
    DOCKER_CMD="sudo docker"
    warn "(Using sudo for this run since you haven't logged out yet to pick up the docker group.)"
fi

# Compose overlay for the registry-pull path. The base file alone has `build:`
# blocks for every service; this overlay nulls them and points at GHCR.
COMPOSE_FILES="-f docker-compose.yml"
if [ "$INSTALL_MODE" = "pull" ]; then
    COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.registry.yml"
    # Surface the chosen tag in .env so subsequent compose calls (e.g. orchestrator
    # spawning new camera slots) inherit the same pin. Default empty -> :latest.
    export IMAGE_TAG="${IMAGE_TAG:-latest}"
    log "Image tag: ${IMAGE_TAG} (override with IMAGE_TAG=vX.Y bash scripts/install-linux.sh)"

    # Persist EXTRA_COMPOSE_FILES + IMAGE_TAG in .env so the orchestrator
    # container (started below by `compose up`) reads them too. Without this,
    # the orchestrator would happily call `docker compose --profile cam2 up -d`
    # with ONLY the base compose file — which still has build: blocks for the
    # cam2 services — and would either silently rebuild or fail.
    sed -i '/^[[:space:]]*#*[[:space:]]*EXTRA_COMPOSE_FILES=/d;/^[[:space:]]*#*[[:space:]]*IMAGE_TAG=/d' .env
    printf '\n# Registry-pull install — orchestrator reads these so cam2-cam20 pull instead of build.\nEXTRA_COMPOSE_FILES=/workspace/docker-compose.registry.yml\nIMAGE_TAG=%s\n' "$IMAGE_TAG" >> .env
fi

if [ "$INSTALL_MODE" = "build" ]; then
    log "Building the shared base image (takes ~3-5 min on first run)..."
    $DOCKER_CMD build -t vision-labs-base:cuda12.8 services/base

    log "Building all service images (takes ~10-15 min on first run)..."
    $DOCKER_CMD compose $COMPOSE_FILES build
else
    log "Pulling pre-built images from ghcr.io/gammahazard/vision-labs/* (takes ~3-5 min, mostly bandwidth)..."
    if ! $DOCKER_CMD compose $COMPOSE_FILES pull 2>&1; then
        fail "Pull failed. If you see a 401/403, the GHCR packages may not be public yet — ask the maintainer, or pass --build to compile locally instead."
    fi
fi

log "Starting the stack..."
$DOCKER_CMD compose $COMPOSE_FILES up -d

# Give services 10 seconds to settle before reporting
sleep 5

heading "All done."
ok "Dashboard:    http://localhost:8080"
ok "Grafana:      http://localhost:3000 (read-only metrics)"
ok "Portainer:    https://localhost:9443 (Docker management UI)"
echo ""
log "First-time setup (in the dashboard):"
echo "    1. Open http://localhost:8080 — login is admin/admin, you'll be forced to change it"
echo "    2. The setup wizard will run: GPU detect, then add your first camera"
echo "    3. Either click 'Scan my network' to auto-find ONVIF cameras, or paste an RTSP URL"
echo ""

if [ "${NEW_DOCKER_GROUP:-0}" = "1" ]; then
    warn "Reminder: log out + back in (or 'newgrp docker') so you don't need sudo for docker commands going forward."
fi
