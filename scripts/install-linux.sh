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
#   7. Builds all service images (base layer first, then everything else)
#   8. Starts the stack with `docker compose up -d`
#   9. Tells you where the dashboard is
#
# USAGE:
#   Inside a cloned vision-labs checkout:
#     bash scripts/install-linux.sh
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

# ---------------------------------------------------------------------------
# Step 6: Build + run
# ---------------------------------------------------------------------------
heading "Step 5/5 — Build + start the stack"

DOCKER_CMD="docker"
if [ "${NEW_DOCKER_GROUP:-0}" = "1" ]; then
    DOCKER_CMD="sudo docker"
    warn "(Using sudo for this run since you haven't logged out yet to pick up the docker group.)"
fi

log "Building the shared base image (takes ~3-5 min on first run)..."
$DOCKER_CMD build -t vision-labs-base:cuda12.8 services/base

log "Building all service images (takes ~10-15 min on first run)..."
$DOCKER_CMD compose build

log "Starting the stack..."
$DOCKER_CMD compose up -d

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
