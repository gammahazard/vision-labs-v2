# Vision Labs — Detailed Guide

In-depth reference for setting up, configuring, and operating Vision Labs. For the project pitch + quick install, see [README.md](README.md). For internal architecture and source-code orientation, see [CONTEXT.md](CONTEXT.md).

---

## Table of contents

1. [What it does](#1-what-it-does)
2. [Architecture overview](#2-architecture-overview)
3. [Service map](#3-service-map)
4. [Manual setup](#4-manual-setup)
5. [First-run wizard](#5-first-run-wizard)
6. [Build vs pre-built images](#6-build-vs-pre-built-images)
7. [Backup + restore](#7-backup--restore)
8. [Hardware tiers + GPU configuration](#8-hardware-tiers--gpu-configuration)
9. [Environment variables](#9-environment-variables)
10. [Dashboard pages](#10-dashboard-pages)
11. [Telegram bot commands](#11-telegram-bot-commands)
12. [AI assistant tools](#12-ai-assistant-tools)
13. [Data flow + Redis schema](#13-data-flow--redis-schema)
14. [Storage layout](#14-storage-layout)
15. [Contracts](#15-contracts)
16. [Security notes](#16-security-notes)
17. [License](#17-license)

---

## 1. What it does

| Feature | Details |
|---------|---------|
| **Person detection** | YOLOv8s-pose detects people with keypoint-based action classification (standing, sitting, crouching, lying) |
| **Face recognition** | InsightFace identifies known people — names stick to bounding boxes even when they turn away |
| **Vehicle tracking** | YOLOv8s detects cars, trucks, buses, motorcycles with idle timer alerts |
| **Telegram notifications** | Real-time photo alerts with AI scene descriptions, broadcast to all approved users |
| **AI assistant** | Qwen 3 14B local LLM with 19 tool functions — query events (with by_type / by_identity / per_camera aggregations), capture live snapshots (auto-described by MiniCPM-V), find DVR segments, send alerts, set reminders. Analytical tools default to `camera="all"` for cross-camera answers. |
| **Vision analysis** | MiniCPM-V multimodal model analyzes camera snapshots and user-uploaded images |
| **DVR recording** | ffmpeg-copy 1-hour `.ts` segments with 3-day rolling retention. Bind-mounted to `./data/recordings/{camera_id}/` so it's browseable from Windows Explorer. Optional QNAP overlay flips the destination to NFS/CIFS |
| **Zone management** | Draw detection/alert/dead zones on the camera view — configurable per time-of-day |
| **Local retention** | Daily prune of `/data/snapshots` and `/data/events` (default 4 days; `SNAPSHOT_RETENTION_DAYS=0` disables) |
| **System monitoring** | Prometheus + Grafana dashboards with GPU, Redis, and inference metrics |

---

## 2. Architecture overview

```
Camera (RTSP)
    │
    ▼
Ingester ──▶ Redis Streams ──▶ YOLO Pose Detector ──▶ Tracker ──▶ Events
                            ──▶ YOLO Vehicle Detector ────────────^
                            ──▶ InsightFace ──▶ Face Identity
                            ──▶ Dashboard (WebSocket ──▶ Browser)

Events ──▶ Notification Poller ──▶ Telegram Bot API
       ──▶ Event Journal (JSONL — local /data/events, or QNAP if enabled)

Ollama (Qwen 3 14B + MiniCPM-V) ◀──▶ Dashboard AI Chat
Recorder (profile-gated) ──▶ DVR segments on QNAP NAS ──▶ Dashboard Playback
Orchestrator ◀──▶ Docker socket ──▶ docker compose up/down --profile camN
Portainer ◀──▶ https://localhost:9443 (Docker management UI)
```

All inter-service communication is via Redis. Cameras are slot-based (`cam1`–`cam20`), each slot gated by a Docker Compose profile, brought up/down by the orchestrator when you add/remove cameras in the dashboard.

For a deeper walk through Redis streams/keys, pub/sub channels, and service-by-service responsibilities, see [CONTEXT.md](CONTEXT.md).

---

## 3. Service map

| Service | GPU | Purpose |
|---------|:---:|---------|
| **redis** | — | Central message bus — all inter-service communication via Redis Streams |
| **camera-ingester** | — | Reads RTSP sub-stream + main stream, publishes JPEG frames to Redis. Hot-reloads `target_fps` from Redis config. Profile-gated per slot |
| **pose-detector** | ✅ `DETECTOR_GPU` | YOLOv8s-pose inference (~44ms), publishes person bounding boxes + keypoints. Profile-gated per slot |
| **vehicle-detector** | ✅ `DETECTOR_GPU` | YOLOv8s inference, publishes vehicle bounding boxes (car/truck/bus/motorcycle). Profile-gated per slot |
| **tracker** | — | IoU matching across frames, assigns persistent IDs, publishes semantic events. Profile-gated per slot |
| **face-recognizer** | ✅ `DETECTOR_GPU` | InsightFace embedding + SQLite enrollment DB, publishes identity matches. Port not exposed on host — access via dashboard proxy at `/api/faces`. Profile-gated per slot |
| **vehicle-attributes** | ✅ `DETECTOR_GPU` | Per-cam HD-crop buffer; flushes per-track dir (`hero.jpg`, `angle_*.jpg`, `metadata.json`) on `vehicle_gone`. Phase 3 classifier (ConvNeXt-Tiny color + body/make/model heads, lazy-fetched from HF Hub) populates `metadata.json.attributes` when `ENABLE_CLASSIFIER=1`. Profile-gated per slot |
| **dashboard** | — | FastAPI backend + static frontend — WebSocket live view (authenticated), REST APIs, background pollers, retention prune. Always on (not profile-gated) |
| **ollama** | ✅ `CHAT_GPU` | Local LLM server — Qwen 3 14B (chat + tools) and MiniCPM-V (vision). Always on |
| **recorder** | — | ffmpeg RTSP→`.ts` copy (no transcode), 1-hour segments, 3-day retention. Default destination `./data/recordings/`. Profile-gated per slot |
| **orchestrator** | — | Watches `cameras:registry` and reconciles compose profiles. Adding a camera via the dashboard auto-runs `docker compose --profile <slot> up -d` (the dashboard itself stays Docker-socket-free for security). Allowed slots: `cam1`–`cam20`. Audits every action to `orchestrator:audit` Redis stream |
| **prometheus** | — | Metrics collection (GPU, Redis, inference timing) |
| **grafana** | — | Monitoring dashboards embedded in the system monitor page |
| **redis-exporter** | — | Exports Redis metrics to Prometheus |
| **dcgm-exporter** | ✅ all | Exports NVIDIA GPU metrics to Prometheus (every visible GPU) |
| **portainer** | — | Web UI for managing the Docker stack at `https://localhost:9443` |

**GPU placement** is driven by two env vars: `DETECTOR_GPU` (pose/vehicle/face) and `CHAT_GPU` (ollama). Defaults are `0` for both — runs on a single GPU. On a dual-GPU rig set `CHAT_GPU=1` to keep the chat LLM off the detector card. The first-run wizard picks sensible values from the probed VRAM/model names; you can also edit `.env` directly. GPU indices match `nvidia-smi -L` order (`CUDA_DEVICE_ORDER=PCI_BUS_ID` is set inside every GPU service).

---

## 4. Manual setup

For users who want full visibility into every step, or to integrate Vision Labs into an existing stack with custom Docker / firewall / volume layout. See [docs/history/MANUAL_SETUP.md](docs/history/MANUAL_SETUP.md) for the very long-form walkthrough; the abridged version is below.

```bash
# 1. Move into WSL2 ext4 (not /mnt/c — bind mounts on 9p are dramatically slower)
mkdir -p ~/projects && cd ~/projects
git clone <repo> vision-labs && cd vision-labs

# 2. Configure environment
cp .env.example .env
# At minimum, set LOCATION_TIMEZONE if you're not in America/Toronto.
# CAMERA + Telegram credentials are NOT in .env any more — the first-run
# wizard at http://localhost:8080/setup.html handles them after start.
# REDIS_PASSWORD is auto-generated by scripts/install-linux.sh; if you're
# running compose manually, append `REDIS_PASSWORD=$(openssl rand -hex 32)`
# to .env or every service will fail to authenticate.

# 3. (Optional) Pick a hardware tier matching your GPU
#    Defaults assume a single 8-12 GB GPU. If yours is smaller or bigger:
cat tiers/small.env >> .env   # 6 GB GPU, no AI chat
# OR
cat tiers/full.env  >> .env   # 16+ GB GPU or dual-GPU rig
# (tiers/mid.env is what the defaults in .env.example already give you)

# 4. Verify GPU passthrough into a container
docker run --rm --gpus all nvidia/cuda:12.8.0-base-ubuntu22.04 nvidia-smi
# Should list all your GPUs (this is the same image the orchestrator
# spawns for the setup wizard's hardware probe)

# 5. Build and run — pick ONE of A or B
#    OPTION A: pull pre-built images from GitHub Container Registry (~3-5 min, mostly pull bandwidth)
docker compose -f docker-compose.yml -f docker-compose.registry.yml pull
docker compose -f docker-compose.yml -f docker-compose.registry.yml up -d
#    Pin a release for reproducibility:
#       IMAGE_TAG=v0.1.0 docker compose -f docker-compose.yml -f docker-compose.registry.yml up -d
#    OPTION B: build locally (~10-15 min, builds base + 9 service images — needed if you've forked + modified code)
#    bash scripts/build.sh              # builds base image first, then everything else
#    docker compose up -d
#    With NAS (overlay stacks with either option above):
docker compose -f docker-compose.yml -f docker-compose.qnap.yml --profile nas up

# 6. Browse
# Dashboard:   http://localhost:8080   (admin/admin on first run — forced password change,
#                                       then a setup wizard runs through GPU detect + first camera)
# Portainer:   https://localhost:9443  (first visit creates admin user)
# Grafana:     http://localhost:3000
# Prometheus:  http://localhost:9090
```

---

## 5. First-run wizard

The first time you open the dashboard on a fresh install, after the forced admin password rotation, you'll be redirected to `/setup.html`. The wizard walks through 7 steps:

1. **Welcome** — what's coming + how to re-run later.
2. **Hardware detect** — orchestrator spawns a one-shot `nvidia-smi` probe, shows your GPUs + recommends a hardware tier (small / mid / full) + estimates how many cameras your VRAM can support. When chat would meaningfully reduce the camera count, a "**I'd rather have more cameras than AI chat**" checkbox appears — flipping it sets `CHAT_MODEL=""` / `VISION_MODEL=""` and reclaims that VRAM for detectors. Click **Apply this configuration** to write the chosen GPU placement + model choices to `.env` and recreate the affected services automatically.
3. **Location + retention** — IANA timezone dropdown (full ~600-zone list, grouped by region) + per-data-type retention day counts (DVR / snapshots / clips). Same `/api/setup/apply-config` path as step 2.
4. **First camera** — Scan your network for ONVIF-compatible cameras, OR enter an RTSP URL manually. Built-in `ffprobe` Test Connection button. Camera ID is auto-assigned to the next free slot (cam1 first). Skip with one click to add cameras later from the Cameras tab — skip jumps you straight to step 6.
5. **Verify** — polls `/api/stats?camera=<id>` for 30 s after the orchestrator spawns the camera's services. ✓ when `frames_in_stream > 0` (pipeline is end-to-end alive), ✗ with a clear pointer to the Cameras tab on timeout. Skippable.
6. **Telegram (optional)** — three substeps designed for someone who's never used Telegram. Paste your bot token from @BotFather → backend validates via `getMe` → instructions tell you to send `/start` to the bot from your phone → backend polls `getUpdates` and captures your `chat_id` + `user_id` automatically → writes the values to `.env` and sends a confirmation message. Skippable. The Telegram page (`/telegram.html`) shows the same inline flow when no token is configured, plus a "Reconnect bot" button for swapping bots later.
7. **Finish** — drops you into the dashboard. State file at `/data/setup-state/setup.json` inside the dashboard's `auth-data` volume marks setup complete.

The step indicator at the top is clickable for any already-visited step; forward jumps are blocked so required state (e.g., camera-add before verify) can't be skipped.

**Pre-existing installs are not force-marched through this wizard** — on startup, if `cameras:registry` already has entries, the dashboard auto-writes the setup-state file with `steps=["preexisting-install"]` and the gate stays open. To re-run the wizard intentionally, delete the state file:
```bash
docker exec vision-labs-dashboard-1 rm /data/setup-state/setup.json
docker compose restart dashboard
```

**ONVIF auto-discovery** is built in. The camera step (and the Cameras tab afterwards) has a "Scan my network" button that finds ONVIF-compatible cameras on a configurable subnet via unicast WS-Discovery probes (no multicast required, works in WSL2). Click a discovered camera, enter credentials, and the wizard pulls the RTSP URL via ONVIF `GetStreamUri` and prefills the form.

Discovery finds: Reolink, Hikvision, Dahua, Amcrest, Axis, Unifi G-series, anything else with ONVIF firmware enabled. **Doesn't find:** DIY setups (Pi + mediamtx, go2rtc, OBS-as-server) because those don't implement ONVIF — they're general-purpose RTSP relays, and need manual URL entry. Same for cameras with ONVIF disabled in firmware (Reolink's default state) — enable ONVIF in the camera's app first, then re-scan.

---

## 6. Build vs pre-built images

Two paths from cloned repo to running stack:

| Path | Time | When to use |
|---|---|---|
| `docker compose -f docker-compose.yml -f docker-compose.registry.yml pull && up -d` | ~3-5 min (pull bandwidth) | **Default.** Pulls finished images from `ghcr.io/gammahazard/vision-labs/*`. Tags are cut by the `publish-images.yml` workflow on every `v*` git tag — both `:vX.Y.Z` and `:latest` are pushed. Pin a release with `IMAGE_TAG=v0.1.0 docker compose -f ... up -d` |
| `bash scripts/build.sh` then `docker compose up -d` | ~10-15 min first time | Build base + 9 service images locally. Use this if you've forked and modified the code, or want to inspect each build layer |

**Cutting a new release** (maintainer workflow):
```bash
git tag -a v0.2.0 -m "v0.2.0 — release notes here"
git push origin v0.2.0    # triggers .github/workflows/publish-images.yml
```
The workflow builds the shared base image first, then the 8 service images in parallel on `ubuntu-latest` runners. ~25-40 min wall-clock. On first publish you'll also need to flip each new package from private to public via GitHub → Packages → ⚙️ → "Change package visibility" (one-time, per package).

**The registry overlay** (`docker-compose.registry.yml`) covers cam1-cam20 — every slot the base compose knows about. Each `build:` block is nulled out so compose pulls instead of building. Volumes, env vars, networks, healthchecks are all inherited from the base file; only the image source changes.

---

## 7. Backup + restore

The face DB, admin DB, Redis state, snapshots, event journal, and Telegram media all live in Docker named volumes. They survive `docker compose down`, `docker compose build`, image rebuilds, and reboots. They do **NOT** survive `docker compose down -v` or `docker volume rm`.

```bash
# Snapshot everything important to a single tarball
bash scripts/backup.sh                            # writes vl-backup-YYYYMMDD-HHMMSS.tar.gz
bash scripts/backup.sh /mnt/external/vl-backup.tar.gz   # custom path

# Restore from a tarball (destructive — overwrites current state)
bash scripts/restore.sh vl-backup-20260518-153000.tar.gz
```

**Backed up:** `face-data` (faces.db + enrolled photos — including all your unknowns), `auth-data`, `redis-data`, `qnap-snapshots`, `qnap-events`, `qnap-telegram`. ~300 MB typical, depending on snapshot retention.

**Not backed up:** DVR recordings (those are a host bind mount at `./data/recordings/` already), YOLO/InsightFace/Ollama model caches (re-downloadable), Prometheus/Grafana metrics history.

### Wipe everything but keep faces

To start fresh on the same host without losing enrolled faces:

```bash
bash scripts/backup.sh                     # tarball ends up in $(pwd)
mv vl-backup-*.tar.gz ~/                   # move outside the project dir

docker compose --profile cam1 --profile cam2 down -v       # wipes ALL volumes
docker rmi $(docker images "vision-labs*" -q) 2>/dev/null  # optional: drop images too
docker image prune -a -f                                   # optional

bash scripts/install-linux.sh              # reinstall (or your prior install method)

bash scripts/restore.sh ~/vl-backup-*.tar.gz   # brings faces, registry, auth, snapshots back
```

After `restore.sh`, your enrolled identities will reappear in the dashboard. Add cameras as usual — the orchestrator will spin up their services using the existing face DB.

---

## 8. Hardware tiers + GPU configuration

Single-GPU is the default. Open `.env` and adjust **`DETECTOR_GPU`** and **`CHAT_GPU`** if you want to split workloads across two cards:

| Variable | Default | What it controls |
|---|---|---|
| `DETECTOR_GPU` | `0` | Which GPU runs pose, vehicle, and face detection |
| `CHAT_GPU` | `0` | Which GPU runs the chat LLM (ollama). Set to `1` for dual-GPU split |
| `POSE_MODEL`, `VEHICLE_MODEL` | `yolov8s-*` | YOLO weight files; swap to `yolov8n-*` for small tier |
| `CHAT_MODEL` | `qwen3:14b` | Ollama model name. Empty string disables AI chat entirely |
| `VISION_MODEL` | `minicpm-v` | Vision LLM. Empty string disables the Vision tab |
| `TARGET_FPS` | `10` | End-to-end frame rate. Drop to 5 on slow GPUs |

GPU indexes match `nvidia-smi -L` order (`CUDA_DEVICE_ORDER=PCI_BUS_ID` is set inside every GPU service so what you see in `nvidia-smi` is what the containers see). On WSL2, both `NVIDIA_VISIBLE_DEVICES` and `CUDA_VISIBLE_DEVICES` are required to isolate a card — the compose file sets both for you.

Switching cards later is two edits + one command:
```bash
# Change CHAT_GPU=0 → CHAT_GPU=1 in .env (or vice versa)
docker compose up -d   # only ollama gets recreated, detectors stay running
```

### Tier presets (`tiers/*.env`)

| Tier | Target VRAM | POSE_MODEL | VEHICLE_MODEL | CHAT_MODEL | VISION_MODEL | TARGET_FPS |
|---|---|---|---|---|---|---|
| `small.env` | 6 GB (1660 Ti, 3050, 2060) | yolov8n-pose.pt | yolov8n.pt | *(empty — off)* | *(empty — off)* | 5 |
| `mid.env` (default) | 8–12 GB (3060, 4060, 4070) | yolov8s-pose.pt | yolov8s.pt | qwen3:7b | *(commented)* | 10 |
| `full.env` | 16+ GB single OR dual-GPU | yolov8s-pose.pt | yolov8s.pt | qwen3:14b | minicpm-v | 15 |

Append a tier to `.env` with `cat tiers/<tier>.env >> .env`.

---

## 9. Environment variables

| Variable | Required | Description |
|----------|:--------:|-------------|
| `REDIS_PASSWORD` | **Yes** | Redis AUTH password. Auto-generated by `scripts/install-linux.sh` on first run (`openssl rand -hex 32`); every service authenticates with it. If empty, Redis runs without AUTH (acceptable for fully isolated 127.0.0.1-only setups, but the install script binds Redis to `127.0.0.1:6379` regardless). |
| `CAM1_RTSP_URL` | Optional | RTSP sub-stream URL for cam1 (overrides the registry entry's `rtsp_sub`). cam2-cam20 use the registry exclusively. The legacy `CAMERA_IP` / `CAMERA_USER` / `CAMERA_PASSWORD` / `RTSP_MAIN` / `RTSP_SUB` env vars were removed from `.env.example` on 2026-05-19 — cameras are added through the wizard / Cameras tab, not by editing env. |
| `CAM1_RTSP_MAIN` | Optional | RTSP main-stream URL for cam1 (same override pattern) |
| `TARGET_FPS` | No | Ingester sub-stream target FPS (default 15; **Redis config `target_fps` overrides this dynamically**) |
| `HD_TARGET_FPS` | No | HD main-stream update rate to Redis (default 8) |
| `JPEG_QUALITY` | No | Sub-stream JPEG quality 1-100 (default 75) |
| `HD_JPEG_QUALITY` | No | HD JPEG quality 1-100 (default 70 — 4K encoding is CPU-heavy) |
| `MAX_STREAM_LEN` | No | Max frames in `frames:*` Redis stream (default 1000) |
| `SNAPSHOT_RETENTION_DAYS` | No | Local prune of `/data/snapshots` + `/data/events` (default 4; set 0 to disable) |
| `TELEGRAM_BOT_TOKEN` | No | Telegram bot token for notifications and commands |
| `TELEGRAM_CHAT_ID` | No | Default Telegram chat ID for alerts |
| `TELEGRAM_ALLOWED_USERS` | No | Comma-separated Telegram user IDs allowed to use the bot |
| `OPENWEATHER_API_KEY` | No | OpenWeatherMap API key for the conditions panel |
| `LOCATION_NAME` | No | Location label shown in dashboard |
| `LOCATION_REGION` | No | Region/state label (for astral library lookup) |
| `LOCATION_LAT` | No | Latitude for sunrise/sunset (default Toronto 43.6532) |
| `LOCATION_LON` | No | Longitude for sunrise/sunset (default Toronto −79.3832) |
| `LOCATION_TIMEZONE` | No | IANA timezone (default `America/Toronto`) |
| `QNAP_ENABLED` | No | Set `true` to layer in the NAS profile (informational; the profile flag is what actually enables it) |
| `QNAP_IP` | No | QNAP NAS IP for CIFS volume mounts (used when `--profile nas`) |
| `QNAP_USER` | No | QNAP login username |
| `QNAP_PASSWORD` | No | QNAP login password |
| `DETECTOR_GPU` | No | GPU index for pose/vehicle/face detectors (default 0) |
| `CHAT_GPU` | No | GPU index for Ollama (default 0; set 1 on dual-GPU) |
| `POSE_MODEL`, `VEHICLE_MODEL` | No | YOLO weight file paths (`/models/yolov8s-pose.pt`, etc.) |
| `CHAT_MODEL` | No | Ollama chat model (default `qwen3:14b`; empty disables) |
| `VISION_MODEL` | No | Ollama vision model (default `minicpm-v`; empty disables) |
| `VEHICLE_RATE_LIMIT_SEC` | No | tracker: min seconds between vehicle events (default 3) |
| `ACTION_DEBOUNCE_FRAMES` | No | tracker: frames a new pose-action must be stable for (default 10) |
| `ACTION_STICKY_MULTIPLIER` | No | tracker: hysteresis multiplier on action changes (default `1` — no stickiness; the previous default of `2` made wrong "sitting" labels stick for ~4s on standing people) |
| `MIN_BBOX_AREA` | No | tracker: minimum detection bbox area in px² (default 3072) |
| `IDENTITY_GRACE_SECONDS` | No | tracker: seconds to wait for face recognition before announcing unknown (default 4.0) |
| `VEHICLE_IOU_THRESHOLD` | No | tracker: IoU for parked-vehicle matching (default 0.2) |
| `MAX_EVENT_STREAM_LEN` | No | tracker: cap on `events:{cam}` stream (default 5000) |
| `MAX_DETECTION_STREAM_LEN` | No | pose/vehicle detectors: cap on detection streams (default 1000) |
| `CACHE_REFRESH_INTERVAL` | No | face-recognizer: seconds between cache reloads from shared DB (default 30) |
| `ALLOWED_PROFILES` | No | orchestrator: profile names it's permitted to up/down (default `cam1,cam2,...,cam20`) |
| `RECONCILE_INTERVAL` | No | orchestrator: safety-net reconcile loop period in seconds (default 10) |

---

## 10. Dashboard pages

### Home (`index.html`) — Multi-camera grid view
The default landing page. Responsive grid of camera tiles — one per registered camera. Each tile streams its own live feed via a dedicated WebSocket (lower FPS to keep many-tile layouts smooth). Click any tile → full-screen modal at full FPS. Below the grid:
- **Conditions** — date, sunrise/sunset, day length, current weather (global)
- **Recent Activity** — combined event feed across every camera, newest-first, each row tagged with a `📷 <camera>` badge. Click an event for details.
- **Known Faces** — face enrollment wizard + gallery (faces are shared across all cameras)

Mobile-responsive: 1 column on phones, 2 on tablets, up to 4 on big screens.

### Detail view (`single.html?camera=<id>`)
Per-camera detailed dashboard. The `?camera=` URL param scopes everything on the page to that camera:
- **Live view** — WebSocket connects with `?camera=<id>`, page title shows the camera's friendly name
- **Bounding boxes**: cyan for identified people, green for unknown, orange for vehicles
- **Face labels**: recognized names with sticky identity (persists when face turns away)
- **Action labels**: classified poses (standing, sitting, crouching, lying)
- **Zone overlays**: drawn zones with color-coded alert levels (per-camera)
- **Settings panel**: confidence, IoU, lost timeout, vehicle confidence, idle timeout, notification toggles — all writes go to `config:{camera_id}` so each camera tunes independently
- **Event feed**: only events from this camera
- **Zone editor**: draw/edit/delete zones for this camera
- **Browse panel**: vehicle snapshots organized by camera/day + enrolled faces gallery

Grid tiles link to `/single.html?camera=<id>` by default. Visiting `/single.html` with no param falls back to the primary camera.

### Cameras (`cameras.html`) — Registry admin
Add / edit / delete / pause cameras. Test RTSP URLs via the Test Connection button (runs ffprobe in the dashboard container). Per-camera detector toggles (Persons / Vehicles / Faces). On Save, the orchestrator sees the registry change via the `cameras:events` Redis pub/sub channel and brings the slot's services up automatically — no terminal command needed. A status pill on each camera row shows live state: `running`, `pending`, `paused`, `stopped`, or `<action> failed`. The pause checkbox flips `enabled: false` in the registry; orchestrator tears down detectors while the registration stays so you can re-enable later. Pre-defined slots: `cam1`–`cam20` (symmetric — all profile-gated and managed by the orchestrator). The real cap is GPU VRAM, not the slot count. To extend beyond 20: duplicate a `camN` block in `docker-compose.yml`, add the new slot to `AVAILABLE_SLOTS` in `services/dashboard/cameras.py`, and append it to `ALLOWED_PROFILES` env on the orchestrator. A future-work item is dynamic slot generation by the orchestrator itself, removing the static cap.

### AI Assistant (`ai.html`)
Three-tab interface:
- **Chat tab**: conversational AI assistant (Qwen 3 14B) with 19 tool functions, all multi-camera-aware. Analytical tools (`query_events`, `query_events_by_date`, `query_event_patterns`, `query_activity_heatmap`, `get_live_scene`, `get_system_status`, `query_notification_history`) default to `camera="all"`. `capture_snapshot` now automatically runs MiniCPM-V on the captured frame and returns a `vision_analysis` field. `find_dvr_segment` returns a clickable deep link to the DVR tab for any historical time window. Tools support filtering by `category` (people / vehicles / faces / actions / security / all) so "only people, no vehicles" questions work correctly.
- **Vision tab**: upload images or capture live frames for standalone MiniCPM-V analysis
- **DVR tab**: browse and play back recorded camera footage with a per-camera Camera dropdown (lists every camera that actually has recordings on disk) plus a date picker; click a segment to play. Supports deep linking via `?tab=recordings&camera=&date=&segment=` URL params (used by `find_dvr_segment` tool). Runs locally on disk by default (no QNAP required).

### System Monitor (`monitoring.html`)
People count, inference time, GPU status, Redis memory cards, plus embedded Grafana dashboard with adjustable time range.

### Telegram Access Manager (`telegram.html`)
If a bot token isn't configured yet, the page shows the **inline connect flow** — paste your @BotFather token, send `/start` to the bot, the dashboard captures your `chat_id` automatically. Once configured, the page becomes the user management UI: approve/revoke bot users with role-based access (admin/user), plus an access log viewer. A "🤖 Bot connection" card with a "Reconnect bot" button lets you swap to a different bot or re-pair from a different Telegram account.

### Login (`login.html`)
Session-based authentication with blurred camera background. **On first login with the default `admin/admin`, the server refuses every route except `/api/auth/change-password` until you rotate.** The session token carries a `must_change` flag so the gate can't be bypassed by a curl client ignoring the UI. Minimum new password length is 8 characters; "admin" is explicitly rejected as a new password.

---

## 11. Telegram bot commands

Most commands accept an optional `[camera]` token at the end: type a camera's id (`cam2`), friendly name (`basement`), an unambiguous prefix (`base`), or `all` to fan out. Bare `/snapshot` or `/clip` with multiple cameras replies with an inline-keyboard tap-to-pick.

| Command | Description |
|---------|-------------|
| `/snapshot [camera]` | 📸 Live photo + AI scene description. No arg + 2+ cams → tap-to-pick picker. `all` = one photo per camera. |
| `/clip [5-40s] [camera]` | 🎬 Video clip with AI analysis. Order-agnostic args. `all` = one clip per camera. |
| `/status [camera]` | 📊 System health. No arg = per-camera breakdown across all cameras. |
| `/who [camera]` | 👁️ Who's in frame. Defaults to **all cameras** (aggregated). |
| `/events [N] [camera]` | 📋 Recent detections (1-20). No camera = merged across cameras with `📷 <name>` prefix on each row. |
| `/zones [camera]` | 🗺️ Snapshot with zone overlays. `all` = one image per camera. |
| `/analyze [camera] [prompt]` | 👁️ MiniCPM-V vision analysis of a live frame. |
| `/cameras` | 📷 List configured cameras with online status + which detectors are enabled. |
| `/timelapse [YYYY-MM-DD] [camera]` | ⏩ Day timelapse from event snapshots. |
| `/rules` | 📜 Notification rules overview (global). |
| `/night` | 🌙 Night override status (global). |
| `/faces` | 👤 Enrolled faces (shared DB, not per-camera). |
| `/ask <question>` | 🧠 Ask the Qwen 3 14B AI assistant directly. |
| `/arm` / `/disarm` | 🟢/🔴 Enable / disable notifications (admin only). |
| `/help` | List available commands. |
| *Send a photo* | Analyze with MiniCPM-V. |

Tab-completion in Telegram clients shows each command's expected args (e.g. `/snapshot 📸 Live photo · [camera]`). The bot is **authorized via `TELEGRAM_ALLOWED_USERS` env + dashboard Telegram Access Manager**. Unauthorized commands are silently dropped and logged. The Telegram update offset is persisted to Redis so dashboard restarts don't replay old commands.

---

## 12. AI assistant tools

The Qwen 3 14B model has access to these function-calling tools:

| Tool | What it does |
|------|-------------|
| `query_events` | Search recent security events by type |
| `query_events_by_date` | Search events for a specific date range |
| `query_event_patterns` | Analyze detection patterns and trends |
| `query_faces` | List enrolled people and recognition stats |
| `query_unknowns` | List unidentified face captures |
| `query_zones` | List configured detection zones |
| `query_notification_history` | Recent Telegram notification log |
| `query_activity_heatmap` | Hourly detection frequency breakdown |
| `browse_vehicles` | Browse vehicle snapshots by date |
| `get_live_scene` | Describe what's currently in the camera frame |
| `get_system_status` | System health and resource usage |
| `get_weather` | Current weather conditions |
| `capture_snapshot` | Take and send a camera snapshot |
| `capture_clip` | Record and send a short video clip |
| `send_telegram` | Send a message to Telegram |
| `schedule_reminder` | Set a timed reminder |
| `show_faces` | Display enrolled face photos inline |
| `analyze_image` | Analyze a specific image with MiniCPM-V |
| `find_dvr_segment` | Resolve camera+date+time → `.ts` segment + deep link to the DVR tab. Used to give the user a clickable link from "show me the clip from yesterday's busiest hour". Does NOT extract or send video bytes. |

---

## 13. Data flow + Redis schema

### Redis Streams (real-time pipeline)

```
frames:cam1                   ← Ingester publishes JPEG frames (sub-stream)
detections:pose:cam1          ← Pose detector publishes person bboxes + keypoints
detections:vehicle:cam1       ← Vehicle detector publishes vehicle bboxes + embedded frame
events:cam1                   ← Tracker publishes semantic events
identities:cam1               ← Face recognizer publishes identity matches
telegram:access_log           ← Bot commands: full audit trail of access attempts
```

Per-camera streams use the slot id as suffix (`cam1`..`cam20`). The dashboard merges across all registered slots when you ask for "all cameras."

### Redis Keys (state)

```
state:cam1                      ← Tracker: current scene snapshot (who's in frame)
identity_state:cam1             ← Face recognizer: currently recognized faces (cleared on empty scene)
config:cam1                     ← Dashboard: live config (thresholds, FPS, cooldowns) — hot-reloaded by services
zones:cam1                      ← Dashboard: zone definitions
frame_hd:cam1                   ← Ingester: latest HD frame, 5s TTL
detection_frame:pose:cam1       ← Pose detector: the exact frame current bboxes were computed from
detection_frame:vehicle:*       ← Vehicle detector: same pattern
cameras:registry                ← Dashboard: hash of registered cameras (id → JSON config)
telegram:users                  ← Dashboard: approved Telegram users
telegram:last_offset            ← Dashboard: persisted Telegram update offset (so restart doesn't replay updates)
person_snapshot:*               ← Tracker: detection-time frame captures (2h TTL)
vehicle_snapshot:*              ← Tracker: vehicle detection snapshots (24h TTL)
```

### Redis pub/sub channels (orchestration triggers)

```
cameras:events       ← Dashboard publishes "added/updated/removed/enabled/disabled"
                       on registry mutation. Orchestrator subscribes.
setup:probe-request  ← Setup wizard asks orchestrator to spawn a one-shot
                       nvidia-smi probe. Reply pushed to setup:probe-result stream.
config:apply         ← Setup wizard asks orchestrator to recreate services
                       after .env edits.
```

For the complete schema (every stream, key, pub/sub channel — who writes, who reads, with line refs), see [CONTEXT.md](CONTEXT.md) §5.

---

## 14. Storage layout

```
./data/recordings/                              ← BIND MOUNT on the WSL host
└── {camera_id}/                                ← (browseable from Windows Explorer
    └── YYYY-MM-DD/                              ← without sudo: \\wsl$\<distro>\...\
        └── HH-MM.ts                            ←  data\recordings\)
                                                ← 1h MPEG-TS segments, 3-day retention

/data/snapshots/                                ← Docker volume qnap-snapshots
├── {camera_id}/{event_id}.jpg                  ← person+event snapshots, per-camera
├── vehicles/{camera_id}/{date}/                ← vehicle snapshots, per-camera+date
└── clips/                                      ← AI + Telegram /clip outputs (3-day)

/data/events/YYYY-MM-DD.jsonl                   ← Docker volume qnap-events
                                                ← every entry tagged with "camera"

/data/telegram/{user}/                          ← Docker volume qnap-telegram
/data/auth.db, /data/ai.db                      ← Docker volume auth-data
/data/faces.db                                  ← Docker volume face-data
```

**Retention defaults** (env-configurable on the dashboard):
- `SNAPSHOT_RETENTION_DAYS=4` → person/vehicle snapshots + event journal
- `CLIP_RETENTION_DAYS=3`     → AI/Telegram clips at `/data/snapshots/clips/`
- `RETENTION_DAYS=3` (recorder) → continuous DVR segments

**With QNAP** (`docker-compose.qnap.yml` overlay): the 6 `qnap-*` volumes flip to CIFS mounts pointing at `//QNAP_IP/vision-labs/<subdir>`. The recordings bind mount can stay local or be swapped to an NFS mount — code paths inside containers (`/recordings/...`) stay identical either way.

### What survives what

| Data | `docker compose down` | `docker compose down -v` | `docker volume rm` | full `rm -rf` of project |
|---|---|---|---|---|
| Enrolled faces (face-data volume) | ✅ | ❌ | ❌ | ❌ |
| Admin password + AI chat history + setup.json (auth-data) | ✅ | ❌ | ❌ | ❌ |
| Camera registry + Redis state (redis-data) | ✅ | ❌ | ❌ | ❌ |
| Snapshots / event JSONL / Telegram media (qnap-*) | ✅ | ❌ | ❌ | ❌ |
| DVR recordings (./data/recordings bind mount) | ✅ | ✅ | ✅ | ❌ |
| Model caches (yolo-models, insightface-models, ollama-models) | ✅ | ❌ | ❌ | ❌ (re-downloadable) |

---

## 15. Contracts

Shared code lives in `contracts/` and is mounted read-only into every service:

| File | Purpose |
|------|---------|
| `streams.py` | Redis stream/key name templates — single source of truth |
| `actions.py` | Keypoint-based action classification (standing, sitting, crouching, lying, arms_raised) |
| `time_rules.py` | Time period calculation (daytime/twilight/night/late_night), zone alert rules, point-in-polygon test |

---

## 16. Security notes

- WebSocket `/ws/live` is authenticated; cookie-less connections are closed with code 4401.
- Face-recognizer port 8081 is **not exposed on the host** — only reachable from inside the bridge net via the dashboard proxy at `/api/faces`.
- Default `admin/admin` credentials force a server-side gate: every route except `/api/auth/change-password` returns 403 until the password is rotated (8-char minimum, "admin" rejected).
- **Brute-force gate on `/api/auth/login`**: 5 failed attempts from one IP in 5 minutes → 15-minute lockout returning HTTP 429 with `Retry-After`. In-memory; resets on container restart.
- Session cookies are HMAC-SHA256-signed; the token now carries the `must_change` flag so the gate can't be bypassed by a non-browser client.
- HTTP logging is set to WARNING for the `httpx` library so Telegram bot tokens don't leak into stdout.
- **Redis is bound to `127.0.0.1:6379`** in docker-compose so LAN devices can't connect — internal-network services reach it via the `redis` hostname, host-network services use localhost. A 32-byte `REDIS_PASSWORD` is auto-generated by `scripts/install-linux.sh` on first install and required by every service.
- Local LAN deployment assumed — Prometheus `/metrics` and Grafana are accessible without auth (acceptable for `localhost`-only; do not port-forward 8080/3000/9090 without adding a reverse proxy + auth).
- Dashboard does **not** mount the Docker socket. Only the orchestrator does. Reduces blast radius if the dashboard is ever compromised.
- The login page background image is server-side blurred (1/4 scale GaussianBlur σ=30 q=30) so it can't be used for surveillance from the unauthenticated `/api/login-bg` endpoint.

---

## 17. License

[MIT](LICENSE) — see the top-level `LICENSE` file for full text.
