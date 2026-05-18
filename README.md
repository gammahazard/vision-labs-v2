# Vision Labs — AI-Powered Security Camera System

> **Real hardware. Real-time inference. Fully self-hosted.**

A self-hosted security platform that processes a live RTSP camera feed through multiple AI models in real-time. Person detection, face recognition, vehicle tracking, and intelligent notifications — all running locally via Docker Compose with zero cloud dependencies.

Built and tested on a dual-GPU workstation (RTX 5070 Ti + RTX 3090) running Ubuntu 24.04 inside WSL2 on Windows. Single-GPU works fine too; the compose file pins GPUs by device_id and is easy to flatten.

---

## What It Does

| Feature | Details |
|---------|---------|
| **Person detection** | YOLOv8s-pose detects people with keypoint-based action classification (standing, sitting, crouching, lying) |
| **Face recognition** | InsightFace identifies known people — names stick to bounding boxes even when they turn away |
| **Vehicle tracking** | YOLOv8s detects cars, trucks, buses, motorcycles with idle timer alerts |
| **Telegram notifications** | Real-time photo alerts with AI scene descriptions, broadcast to all approved users |
| **AI assistant** | Qwen 3 14B local LLM with 18 tool functions — query events, send alerts, capture snapshots, set reminders |
| **Vision analysis** | MiniCPM-V multimodal model analyzes camera snapshots and user-uploaded images |
| **DVR recording** | ffmpeg-copy 1-hour `.ts` segments with 3-day rolling retention. Runs by default to `./data/recordings/{camera_id}/` (bind-mounted on the WSL host so it's browseable from Windows Explorer). Optional QNAP overlay flips the destination to NFS/CIFS |
| **Zone management** | Draw detection/alert/dead zones on the camera view — configurable per time-of-day |
| **Local retention** | Daily prune of `/data/snapshots` and `/data/events` (default 4 days, configurable). Disabled by setting `SNAPSHOT_RETENTION_DAYS=0` |
| **System monitoring** | Prometheus + Grafana dashboards with GPU, Redis, and inference metrics |

---

## Architecture

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
Portainer ◀──▶ https://localhost:9443 (Docker management UI)
```

### Services

| Service | GPU | Purpose |
|---------|:---:|---------|
| **redis** | — | Central message bus — all inter-service communication via Redis Streams |
| **camera-ingester** | — | Reads RTSP sub-stream + main stream, publishes JPEG frames to Redis. Hot-reloads `target_fps` from Redis config |
| **pose-detector** | ✅ GPU 0 (5070 Ti) | YOLOv8s-pose inference (~44ms), publishes person bounding boxes + keypoints |
| **vehicle-detector** | ✅ GPU 0 (5070 Ti) | YOLOv8s inference, publishes vehicle bounding boxes (car/truck/bus/motorcycle) |
| **tracker** | — | IoU matching across frames, assigns persistent IDs, publishes semantic events |
| **face-recognizer** | ✅ GPU 0 (5070 Ti) | InsightFace embedding + SQLite enrollment DB, publishes identity matches. **Port not exposed on host** — access via dashboard proxy at `/api/faces` |
| **dashboard** | — | FastAPI backend + static frontend — WebSocket live view (authenticated), REST APIs, background pollers, retention prune |
| **ollama** | ✅ GPU 1 (3090) | Local LLM server — Qwen 3 14B (chat + tools) and MiniCPM-V (vision) |
| **recorder** | — | ffmpeg RTSP→`.ts` copy (no transcode), 1-hour segments, 3-day retention. Runs by default to `./data/recordings/`; cam2-cam5 recorders are profile-gated and managed by the orchestrator |
| **orchestrator** | — | Watches `cameras:registry` and reconciles compose profiles. When you add a camera via the dashboard, this service auto-runs `docker compose --profile <slot> up -d` (the dashboard itself stays Docker-socket-free for security). Slots: `cam2`–`cam5`. Audits every action to `orchestrator:audit` Redis stream |
| **prometheus** | — | Metrics collection (GPU, Redis, inference timing) |
| **grafana** | — | Monitoring dashboards embedded in the system monitor page |
| **redis-exporter** | — | Exports Redis metrics to Prometheus |
| **dcgm-exporter** | ✅ both | Exports NVIDIA GPU metrics to Prometheus |
| **portainer** | — | Web UI for managing the Docker stack at `https://localhost:9443` |

**GPU split** (configurable in `docker-compose.yml`): the always-on detector trio runs on GPU 0; the heavy on-demand workload (Ollama) gets GPU 1's headroom. Verify the device_ids match `nvidia-smi -L` output if you have a different physical layout.

---

## Requirements

- **NVIDIA GPU(s)** with driver supporting CUDA 12.8 (R555+). Tested on RTX 5070 Ti (Blackwell sm_120) + RTX 3090 (Ampere sm_86)
- **Docker Engine inside WSL2** (Ubuntu 24.04 recommended) — NOT Docker Desktop. See [MANUAL_SETUP.md](MANUAL_SETUP.md) for the install steps
- [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) configured for Docker
- **RTSP-capable IP camera** (tested with Reolink RLC-1240A over PoE)
- **QNAP NAS** (optional — only needed for DVR + persistent NAS storage)

### Setup (short version — see MANUAL_SETUP.md for the full walkthrough)

```bash
# 1. Move into WSL2 ext4 (not /mnt/c — bind mounts on 9p are dramatically slower)
mkdir -p ~/projects && cd ~/projects
git clone <repo> vision-labs && cd vision-labs

# 2. Configure environment
cp .env.example .env
# Edit .env with your camera IP, credentials, Telegram bot token, etc.

# 3. (Optional) Pick a hardware tier matching your GPU
#    Defaults assume a single 8-12 GB GPU. If yours is smaller or bigger:
cat tiers/small.env >> .env   # 6 GB GPU, no AI chat
# OR
cat tiers/full.env  >> .env   # 16+ GB GPU or dual-GPU rig
# (tiers/mid.env is what the defaults in .env.example already give you)

# 4. Verify GPU passthrough into a container
docker run --rm --gpus all nvidia/cuda:12.8.0-base-ubuntu24.04 nvidia-smi
# Should list all your GPUs

# 5. Build and run
docker compose build               # ~10-20 min on first build
docker compose up                  # without NAS
# OR
docker compose -f docker-compose.yml -f docker-compose.qnap.yml --profile nas up
                                   # with NAS (enables the recorder service)

# 6. Browse
# Dashboard:   http://localhost:8080   (admin/admin on first run — you'll be forced to set a new password)
# Portainer:   https://localhost:9443  (first visit creates admin user)
# Grafana:     http://localhost:3000
# Prometheus:  http://localhost:9090
```

### Hardware tiers + GPU configuration

Single-GPU is the default. Open `.env` and adjust **`DETECTOR_GPU`** and **`CHAT_GPU`** if you want to split workloads across two cards:

| Variable | Default | What it controls |
|---|---|---|
| `DETECTOR_GPU` | `0` | Which GPU runs pose, vehicle, and face detection |
| `CHAT_GPU` | `0` | Which GPU runs the chat LLM (ollama). Set to `1` for dual-GPU split |
| `POSE_MODEL`, `VEHICLE_MODEL` | `yolov8s-*` | YOLO weight files; swap to `yolov8n-*` for small tier |
| `CHAT_MODEL` | `qwen3:14b` | Ollama model name. Empty string disables AI chat entirely |
| `VISION_MODEL` | `minicpm-v` | Vision LLM. Empty string disables the Vision tab |
| `TARGET_FPS` | `10` | End-to-end frame rate. Drop to 5 on slow GPUs |

GPU indexes match `nvidia-smi -L` order (we set `CUDA_DEVICE_ORDER=PCI_BUS_ID` inside every GPU service so what you see in `nvidia-smi` is what the containers see). On WSL2, both `NVIDIA_VISIBLE_DEVICES` and `CUDA_VISIBLE_DEVICES` are required to isolate a card — the compose file sets both for you.

Switching cards later is two edits + one command:
```bash
# Change CHAT_GPU=0 → CHAT_GPU=1 in .env (or vice versa)
docker compose up -d   # only ollama gets recreated, detectors stay running
```

### Environment Variables

| Variable | Required | Description |
|----------|:--------:|-------------|
| `CAMERA_IP` | Yes | IP address of the RTSP camera |
| `CAMERA_USER` | Yes | Camera login username |
| `CAMERA_PASSWORD` | Yes | Camera login password |
| `RTSP_MAIN` | Auto | Built from CAMERA_* in `.env`; HD main stream URL |
| `RTSP_SUB` | Auto | Built from CAMERA_* in `.env`; SD sub-stream URL |
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
| `LOCATION_LAT` | No | Latitude for sunrise/sunset (default Toronto) |
| `LOCATION_LON` | No | Longitude for sunrise/sunset (default Toronto) |
| `LOCATION_TIMEZONE` | No | IANA timezone (default: `America/Toronto`) |
| `QNAP_ENABLED` | No | Set `true` to layer in the NAS profile (informational; the profile flag is what actually enables it) |
| `QNAP_IP` | No | QNAP NAS IP for CIFS volume mounts (used when `--profile nas`) |
| `QNAP_USER` | No | QNAP login username |
| `QNAP_PASSWORD` | No | QNAP login password |
| `VEHICLE_RATE_LIMIT_SEC` | No | tracker: min seconds between vehicle events (default 3) |
| `ACTION_DEBOUNCE_FRAMES` | No | tracker: frames a new pose-action must be stable for (default 10) |
| `ACTION_STICKY_MULTIPLIER` | No | tracker: hysteresis multiplier on action changes (default 2) |
| `MIN_BBOX_AREA` | No | tracker: minimum detection bbox area in px² (default 3072) |
| `IDENTITY_GRACE_SECONDS` | No | tracker: seconds to wait for face recognition before announcing unknown (default 4.0) |
| `VEHICLE_IOU_THRESHOLD` | No | tracker: IoU for parked-vehicle matching (default 0.2) |
| `MAX_EVENT_STREAM_LEN` | No | tracker: cap on `events:{cam}` stream (default 5000) |
| `MAX_DETECTION_STREAM_LEN` | No | pose/vehicle detectors: cap on detection streams (default 1000) |
| `CACHE_REFRESH_INTERVAL` | No | face-recognizer: seconds between cache reloads from shared DB (default 30) |
| `ALLOWED_PROFILES` | No | orchestrator: profile names it's permitted to up/down (default `cam2,cam3,cam4,cam5`) |
| `RECONCILE_INTERVAL` | No | orchestrator: safety-net reconcile loop period in seconds (default 10) |

---

## Dashboard Pages

### Home (`index.html`) — Multi-camera grid view
The default landing page. Responsive grid of camera tiles — one per registered camera. Each tile streams its own live feed via a dedicated WebSocket (lower FPS to keep many-tile layouts smooth). Click any tile → full-screen modal at full FPS. Below the grid:
- **Conditions** — date, sunrise/sunset, day length, current weather (global)
- **📋 Recent Activity** — combined event feed across every camera, newest-first, each row tagged with a `📷 <camera>` badge so you can see at a glance which camera triggered what. Click an event for details.
- **Known Faces** — face enrollment wizard + gallery (faces are shared across all cameras)

Mobile-responsive: 1 column on phones, 2 on tablets, up to 4 on big screens.

### Detail view (`single.html?camera=<id>`)
Per-camera detailed dashboard. The `?camera=` URL param scopes everything on the page to that camera:
- **Live view** — WebSocket connects with `?camera=<id>`, page title shows the camera's friendly name (e.g. "Live View — basement")
- **Bounding boxes**: cyan for identified people, green for unknown, orange for vehicles
- **Face labels**: recognized names with sticky identity (persists when face turns away)
- **Action labels**: classified poses (standing, sitting, crouching, lying)
- **Zone overlays**: drawn zones with color-coded alert levels (per-camera)
- **Settings panel**: confidence, IoU, lost timeout, vehicle confidence, idle timeout, notification toggles — all writes go to `config:{camera_id}` so each camera tunes independently
- **Event feed**: only events from this camera (no badge needed since context is obvious)
- **Zone editor**: draw/edit/delete zones for this camera
- **Browse panel**: vehicle snapshots organized by camera/day + enrolled faces gallery

Grid tiles link to `/single.html?camera=<id>` by default. Visiting `/single.html` with no param falls back to the primary camera.

### Cameras (`cameras.html`) — Registry admin
Add / edit / delete / pause cameras. Test RTSP URLs via the **Test Connection** button (runs ffprobe in the dashboard container). Per-camera detector toggle (Persons / Vehicles / Faces). On Save, the **orchestrator** sees the registry change via the `cameras:events` Redis pub/sub channel and brings the slot's services up automatically — no terminal command needed. A status pill on each camera row shows live state: `running`, `pending`, `paused`, `stopped`, or `<action> failed`. The pause checkbox flips `enabled: false` in the registry; orchestrator tears down detectors while the registration stays so you can re-enable later. Pre-defined slots: `cam2`–`cam5` (extend by duplicating a block in `docker-compose.yml` and adding to `AVAILABLE_SLOTS` in `services/dashboard/cameras.py` + `ALLOWED_PROFILES` env on the orchestrator).

### AI Assistant (`ai.html`)
Three-tab interface:
- **Chat tab**: conversational AI assistant (Qwen 3 14B) with 18 tool functions, all multi-camera-aware (LLM can call any tool with a specific `camera` arg or `"all"` for system-wide queries)
- **Vision tab**: upload images or capture live frames for MiniCPM-V analysis
- **DVR tab**: browse and play back recorded camera footage with a per-camera **Camera** dropdown (lists every camera that actually has recordings on disk) plus a date picker; click a segment to play. Runs locally on disk by default (no QNAP required).

### System Monitor (`monitoring.html`)
People count, inference time, GPU status, Redis memory cards, plus embedded Grafana dashboard with adjustable time range.

### Telegram Access Manager (`telegram.html`)
Approve/revoke bot users with role-based access (admin/user). Access log viewer for all incoming bot interactions.

### Login (`login.html`)
Session-based authentication with blurred camera background. **On first login with the default `admin/admin`, the UI forces a password rotation before letting you into the dashboard.**

---

## Telegram Bot Commands

Most commands accept an optional `[camera]` token at the end: type a camera's
id (`cam2`), friendly name (`basement`), an unambiguous prefix (`base`), or
`all` to fan out. Bare `/snapshot` or `/clip` with multiple cameras replies
with an inline-keyboard tap-to-pick.

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

Tab-completion in Telegram clients shows each command's expected args (e.g.
`/snapshot 📸 Live photo · [camera]`). The bot is **authorized via
`TELEGRAM_ALLOWED_USERS` env + dashboard Telegram Access Manager**.
Unauthorized commands are silently dropped and logged. The Telegram update
offset is persisted to Redis so dashboard restarts don't replay old commands.

---

## AI Assistant Tools (18)

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

---

## Data Flow

### Redis Streams (real-time pipeline)

```
frames:front_door             ← Ingester publishes JPEG frames (sub-stream)
detections:pose:front_door    ← Pose detector publishes person bboxes + keypoints
detections:vehicle:front_door ← Vehicle detector publishes vehicle bboxes + embedded frame
events:front_door             ← Tracker publishes semantic events
identities:front_door         ← Face recognizer publishes identity matches
telegram:access_log           ← Bot commands: full audit trail of access attempts
```

### Redis Keys (state)

```
state:front_door                ← Tracker: current scene snapshot (who's in frame)
identity_state:front_door       ← Face recognizer: currently recognized faces (cleared on empty scene)
config:front_door               ← Dashboard: live config (thresholds, FPS, cooldowns) — hot-reloaded by services
zones:front_door                ← Dashboard: zone definitions
frame_hd:front_door             ← Ingester: latest HD frame, 5s TTL
detection_frame:pose:front_door ← Pose detector: the exact frame current bboxes were computed from
detection_frame:vehicle:*       ← Vehicle detector: same pattern
telegram:users                  ← Dashboard: approved Telegram users
telegram:last_offset            ← Dashboard: persisted Telegram update offset (so restart doesn't replay updates)
person_snapshot:*               ← Tracker: detection-time frame captures (2h TTL)
vehicle_snapshot:*              ← Tracker: vehicle detection snapshots (24h TTL)
```

### Storage Layout

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
/data/auth.db, /data/ai.db, /data/faces.db      ← Docker volumes (separate)
```

**Retention defaults** (env-configurable on the dashboard):
- `SNAPSHOT_RETENTION_DAYS=4` → person/vehicle snapshots + event journal
- `CLIP_RETENTION_DAYS=3`     → AI/Telegram clips at `/data/snapshots/clips/`
- `RETENTION_DAYS=3` (recorder) → continuous DVR segments

**With QNAP** (`docker-compose.qnap.yml` overlay): the 6 `qnap-*` volumes
flip to CIFS mounts pointing at `//QNAP_IP/vision-labs/<subdir>`. The
recordings bind mount can stay local or be swapped to an NFS mount — code
paths inside containers (`/recordings/...`) stay identical either way.

---

## Contracts

Shared code lives in `contracts/` and is mounted read-only into every service:

| File | Purpose |
|------|---------|
| `streams.py` | Redis stream/key name templates — single source of truth |
| `actions.py` | Keypoint-based action classification (standing, sitting, crouching, lying, arms_raised) |
| `time_rules.py` | Time period calculation (daytime/twilight/night/late_night), zone alert rules, point-in-polygon test |

---

## Security notes

- WebSocket `/ws/live` is authenticated; cookie-less connections are closed with code 4401
- Face-recognizer port 8081 is **not exposed on the host** — only reachable from inside the bridge net via the dashboard proxy at `/api/faces`
- Default `admin/admin` credentials force a password change on first login
- HTTP logging is set to WARNING for the `httpx` library so Telegram bot tokens don't leak into stdout
- Local LAN deployment assumed — Prometheus `/metrics` and Grafana are accessible without auth (acceptable for `localhost`-only; do not port-forward 8080/3000/9090 without adding a reverse proxy + auth)

---

## License

This project is for personal/educational use.
