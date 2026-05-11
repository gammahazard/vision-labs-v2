# Vision Labs — Architecture Reference

> **Last updated:** May 10, 2026 (post-migration to WSL2 + dual-GPU, post-security-hardening pass)

---

## Table of Contents

1. [System Overview](#system-overview)
2. [Service Map](#service-map)
3. [Data Flow Pipelines](#data-flow-pipelines)
4. [Redis Schema](#redis-schema)
5. [Dashboard Backend](#dashboard-backend)
6. [Dashboard Frontend](#dashboard-frontend)
7. [Tracker Service](#tracker-service)
8. [Face Recognition](#face-recognition)
9. [AI Assistant](#ai-assistant)
10. [Image Generation](#image-generation)
11. [Notification System](#notification-system)
12. [Telegram Bot](#telegram-bot)
13. [DVR Recording](#dvr-recording)
14. [Zone System](#zone-system)
15. [Monitoring Stack](#monitoring-stack)
16. [Shared Contracts](#shared-contracts)
17. [Authentication](#authentication)
18. [NAS Storage Layout](#nas-storage-layout)
19. [Docker Infrastructure](#docker-infrastructure)
20. [File Index](#file-index)

---

## System Overview

Vision Labs is an **event-driven microservice system** running on a single host via Docker Compose, with two GPUs pinned by `device_ids`. A Reolink PoE camera provides an RTSP video feed that flows through a pipeline of AI models, with results displayed in a web dashboard and sent as Telegram notifications.

```
┌──────────────────────────────────────────────────────────────────────┐
│  Windows host + WSL2 Ubuntu 24.04 — Docker Engine, NOT Desktop       │
│  GPU 0: RTX 5070 Ti (16 GB, Blackwell sm_120)                        │
│  GPU 1: RTX 3090     (24 GB, Ampere   sm_86)                         │
│                                                                      │
│  ┌─────────────────┐                                                 │
│  │  Camera (RTSP)  │──(sub-stream ~896×512)──▶ Ingester ──▶ Redis    │
│  │  Reolink PoE    │──(main-stream HD)───────▶ Ingester ──▶ Redis    │
│  └─────────────────┘                                                 │
│                                                                      │
│  Redis Streams ──▶ Pose Detector       (GPU 0 — YOLOv8s-pose)        │
│                ──▶ Vehicle Detector    (GPU 0 — YOLOv8s)             │
│                ──▶ Face Recognizer     (GPU 0 — InsightFace)         │
│                         │                                            │
│                         ▼                                            │
│                    Tracker (CPU) ──▶ Events Stream                   │
│                                                                      │
│           ┌───────────────────────────────┐                          │
│           │         Dashboard             │                          │
│           │  FastAPI :8080                 │                          │
│           │  WebSocket /ws/live (authed)   │                          │
│           │  REST API /api/* (cookie)      │                          │
│           │  Static frontend              │──▶ Telegram Bot API      │
│           │  Event poller + retention     │                          │
│           └───────────────────────────────┘                          │
│                    │              │                                   │
│            Ollama (GPU 1)    ComfyUI (GPU 1)                        │
│            Qwen 3 14B        SDXL                                    │
│            MiniCPM-V                                                 │
│                                                                      │
│  Recorder (profile-gated, off by default) ──▶ QNAP NAS               │
│  Prometheus + Grafana + Portainer ──▶ Ops UIs                        │
└──────────────────────────────────────────────────────────────────────┘
```

**GPU split rationale:** the three always-on detectors total <4 GB VRAM and fit comfortably on the 5070 Ti. Ollama + ComfyUI are heavy on-demand workloads that benefit from the 3090's 24 GB headroom — and ComfyUI's `gpu:generation_active` flag pauses all three detectors *and* face-recognizer when generation starts.

---

## Service Map

| Service | Container | Host port | GPU | Description |
|---------|-----------|-----------|:---:|-------------|
| **redis** | redis:7-alpine | 6379 | — | Central message bus, AOF persistence, 2GB maxmemory |
| **camera-ingester** | custom | host net | — | RTSP→JPEG frames, publishes sub-stream + HD to Redis. Hot-reloads `target_fps` from Redis config |
| **pose-detector** | custom | — | GPU 0 | YOLOv8s-pose, publishes person bboxes + keypoints. Honors `gpu:generation_active` |
| **vehicle-detector** | custom | — | GPU 0 | YOLOv8s, publishes vehicle bboxes. Honors `gpu:generation_active` |
| **tracker** | custom | — | — | IoU-based person/vehicle tracking, event publishing |
| **face-recognizer** | custom | *internal only (8081 not exposed)* | GPU 0 | InsightFace embedding, SQLite DB, REST API proxied through dashboard. Honors `gpu:generation_active` |
| **dashboard** | custom | 8080 | — | FastAPI + WebSocket + static files + background tasks. WebSocket `/ws/live` requires session cookie |
| **ollama** | ollama/ollama | 11434 | GPU 1 | LLM inference (Qwen 3 14B, MiniCPM-V) |
| **comfyui** | custom | 8188 | GPU 1 | Stable Diffusion image generation |
| **recorder** | custom | host net | — | RTSP→`.ts` ffmpeg copy, 1-hour segments. **Profile-gated `nas` — off by default** |
| **prometheus** | prom/prometheus | 9090 (host net) | — | Metrics collection, 30d retention |
| **grafana** | grafana/grafana-oss | 3000 (host net) | — | Monitoring dashboards, provisioned from `services/grafana/dashboards/vision-labs.json` |
| **redis-exporter** | redis_exporter | host net | — | Redis metrics → Prometheus |
| **dcgm-exporter** | dcgm-exporter | host net | all | NVIDIA GPU metrics → Prometheus |
| **portainer** | portainer/portainer-ce | 9000 (HTTP), 9443 (HTTPS) | — | Docker management UI |

---

## Data Flow Pipelines

### Detection Pipeline (real-time, ~5-10 FPS — limited by camera sub-stream rate)

```
1. camera-ingester:
   - OpenCV reads RTSP sub-stream at TARGET_FPS (default 15 env, hot-reloaded from Redis config:front_door.target_fps)
   - Forces TCP transport (OPENCV_FFMPEG_CAPTURE_OPTIONS set BEFORE cv2 import)
   - JPEG encode each frame at JPEG_QUALITY (default 75)
   - XADD to frames:front_door (capped at MAX_STREAM_LEN=1000)
   - Separate thread: reads main stream at HD_TARGET_FPS (default 8),
     SETEX frame_hd:front_door with 5s TTL

2. pose-detector (consumer group "pose_detectors"):
   - Check gpu:generation_active — if set, sleep 2s and continue
   - XREADGROUP from frames stream
   - YOLOv8s-pose inference (~44ms on RTX 5070 Ti)
   - Filter: person class only, confidence > threshold
   - SET detection_frame:pose:front_door = the frame JPEG used
   - XADD detections:pose:front_door {detections: JSON, inference_ms}

3. vehicle-detector (consumer group "vehicle_detectors"):
   - Same pattern as pose-detector
   - Classes: car, truck, bus, motorcycle
   - Embeds `frame_bytes` directly in detection messages (load-bearing for tracker
     vehicle snapshots — captures the exact frame the bbox was computed from)
   - SET detection_frame:vehicle:front_door = frame used
   - XADD detections:vehicle:front_door

4. tracker (consumer groups "trackers" + "vehicle_trackers"):
   - XREADGROUP from detections:pose + detections:vehicle
   - IoU matching against tracked persons/vehicles
   - If new person: debounce 15 frames, then emit person_appeared
   - If person gone > lost_timeout: emit person_left
   - If face recognized: emit person_identified
   - If vehicle stationary > vehicle_idle_timeout: emit vehicle_idle
   - HSET state:front_door with current scene snapshot
   - XADD events:front_door {event_type, person_id, bbox, zone, ...}
   - SETEX person_snapshot:{camera}:{ts} = frame JPEG (2h TTL)
   - SETEX vehicle_snapshot:{camera}:{ts} = frame JPEG (24h TTL)

5. face-recognizer (consumer group "face_recognizers"):
   - Check gpu:generation_active — if set, sleep 2s and continue
   - XREADGROUP from detections:pose stream (only runs face matching on frames where pose detected a person)
   - XREVRANGE frames stream to grab the matching frame
   - InsightFace detection + embedding extraction (CUDA via onnxruntime-gpu)
   - Compare against SQLite DB of enrolled faces (cosine similarity)
   - HSET identity_state:front_door with matches
   - If detection list is empty: DEL identity_state:front_door (avoids stale labels on empty scenes)
   - XADD identities:front_door

6. dashboard WebSocket (/ws/live — authenticated, validates vl_session cookie):
   - Read latest frame from detection_frame:pose:front_door
   - Read state:front_door for current persons/vehicles
   - Read identity_state:front_door for face labels
   - Read zones:front_door for zone overlays
   - Draw bounding boxes, keypoints, face labels, zone overlays
   - JPEG encode at q=85 → base64 → send JSON via WebSocket
   - Render rate hot-reloaded from config:front_door.target_fps (default 10)
```

### Event Notification Flow

```
1. tracker emits event to events:front_door
2. dashboard _event_notification_poller (background thread):
   - XREAD events stream (blocking, in threadpool executor)
   - For each event:
     a. Save snapshot JPEG to /data/snapshots/ (always)
     b. Journal event to /data/events/YYYY-MM-DD.jsonl (always)
     c. If Telegram configured and not rate-limited:
        - Draw bbox on HD frame
        - Run MiniCPM-V scene description
        - Build caption (event type, time, zone, AI description)
        - Broadcast photo to all approved Telegram users
```

### Face Recognition Flow

```
1. face-recognizer runs InsightFace on each frame
2. For each detected face: extract 512-dim embedding
3. Compare against all enrolled faces in SQLite (cosine similarity)
4. If similarity > 0.5: publish identity to identity_state:{camera}
5. Tracker reads identity_state, links name to tracked person_id
6. Dashboard reads identity_state, draws name labels on bboxes
7. Sticky identity: once recognized, name persists even when face turns away
   (10-frame vote buffer with 2x bias for current identity prevents flicker)
```

### Face Enrollment Flow

```
1. User types name in dashboard wizard, clicks Capture
2. Browser → POST /api/faces/enroll {name}
3. Dashboard proxies to face-recognizer:8081/api/faces/enroll
4. Face-recognizer:
   - XREVRANGE frames (latest 1) → full frame
   - Pick largest person bbox from current detections
   - Crop upper 50% → InsightFace → embedding + portrait thumbnail
   - INSERT into SQLite: {name, embedding_blob, portrait_jpeg}
   - Return success + face_id
```

---

## Redis Schema

### Streams

| Key Pattern | Producer | Consumer | Payload |
|-------------|----------|----------|---------|
| `frames:{camera_id}` | camera-ingester | pose-detector, vehicle-detector, face-recognizer | `{frame, timestamp, frame_number, width, height}` |
| `detections:pose:{camera_id}` | pose-detector | tracker | `{detections: JSON, inference_ms, timestamp}` |
| `detections:vehicle:{camera_id}` | vehicle-detector | tracker | `{detections: JSON, inference_ms, timestamp}` |
| `events:{camera_id}` | tracker | dashboard poller | `{event_type, person_id, bbox, zone, alert_level, ...}` |
| `identities:{camera_id}` | face-recognizer | dashboard | `{identities: JSON}` |
| `telegram:access_log` | bot_commands | dashboard (Telegram page) | `{user_id, username, action, authorized, timestamp}` |

### Keys (state)

| Key Pattern | Writer | Reader | Content |
|-------------|--------|--------|---------|
| `state:{camera_id}` | tracker | dashboard WebSocket | `{num_people, people: JSON[{person_id, bbox, action}]}` |
| `identity_state:{camera_id}` | face-recognizer | dashboard WebSocket | `{face_id: {name, confidence, bbox}}` |
| `config:{camera_id}` | dashboard settings | pose-detector, tracker, vehicle-detector | `{confidence_thresh, iou_threshold, lost_timeout, ...}` |
| `zones:{camera_id}` | dashboard zone editor | tracker, dashboard overlay | `{zone_id: JSON{name, points, alert_level}}` |
| `frame_hd:{camera_id}` | camera-ingester | dashboard (snapshots) | Raw JPEG bytes (HD frame) |
| `detection_frame:{type}:{camera_id}` | pose/vehicle detector | dashboard WebSocket | Raw JPEG bytes (the frame bboxes were computed from) |
| `person_snapshot:{camera_id}:{ts}` | tracker | dashboard event feed | Raw JPEG bytes (2-hour TTL) |
| `vehicle_snapshot:{camera_id}:{ts}` | tracker | dashboard browse/events | Raw JPEG bytes (24-hour TTL) |
| `gpu:generation_active` | image_gen | pose-detector, vehicle-detector, face-recognizer | Lock flag — detectors pause GPU when present |
| `gpu:generation_lock` | image_gen | image_gen | Mutex preventing concurrent generations. Cleared on dashboard startup |
| `telegram:users` | dashboard (Telegram Access Manager) | bot_commands | `{user_id: JSON{chat_id, name, role, approved_at}}` |
| `telegram:last_offset` | bot_commands | bot_commands | Persisted Telegram update offset — restored at startup so restarts don't replay old commands |

### Config Keys (in `config:{camera_id}`)

| Field | Default | Description |
|-------|---------|-------------|
| `confidence_thresh` | `0.5` | YOLO detection confidence minimum |
| `iou_threshold` | `0.3` | IoU overlap threshold for tracking |
| `lost_timeout` | `5.0` | Seconds before marking person as left |
| `target_fps` | `10` | End-to-end FPS — drives both camera-ingester rate AND dashboard WebSocket render rate (both hot-reload) |
| `notify_person` | `1` | Enable person detection notifications |
| `notify_vehicle` | `1` | Enable vehicle detection notifications |
| `suppress_known` | `0` | Suppress notifications for recognized people |
| `notify_cooldown` | `60` | Seconds between person notifications |
| `vehicle_cooldown` | `60` | Seconds between vehicle notifications |
| `vehicle_confidence_thresh` | `0.35` | Vehicle detection confidence minimum |
| `vehicle_idle_timeout` | `90` | Seconds before vehicle idle alert |

---

## Dashboard Backend

### `server.py` (353 lines — post May 2026 refactor)

The main FastAPI app is now a **thin wiring file**. The 1300-line original was split into 6 modules:

| Component | Lives in | Purpose |
|-----------|----------|---------|
| `auth_middleware` | server.py:~200 | Session-based auth, redirects (303) unauthenticated to login |
| `login_background` | server.py:~240 | Heavily blurred camera snapshot for login page (no auth) |
| `startup` event | server.py:~270 | Init auth DB, write default config, seed camera registry, schedule pollers |
| `reminder_poller` | `pollers/reminders.py` | Check due reminders every 60s, send via Telegram |
| `warm_ollama` | `pollers/ollama_warmup.py` | Pull Qwen 3 14B on first startup, warm-up GPU load |
| `clear_comfyui_queue` | `pollers/comfyui_cleanup.py` | Clear stale GPU locks + ComfyUI queue from previous session |
| `retention_poller` | `pollers/retention.py` | Daily prune of `/data/snapshots` + `/data/events` |
| `event_notification_poller` | `pollers/events.py` | Poll events, save snapshots, journal, send Telegram |
| `websocket_live` | `websocket.py` | Stream frames with overlays; accepts `?camera=<id>` for multi-camera |

### Route Modules (in `routes/`)

Routes marked **✅ multi-camera** accept an optional `?camera=<id>` query
parameter. Empty/missing camera falls back to the dashboard's primary camera
(env `CAMERA_ID`); the literal value `all` aggregates across every enabled
camera (where it makes semantic sense — events, system status).

| Module | Prefix | Purpose | Multi-camera |
|--------|--------|---------|---|
| `ai.py` | `/api/ai` | Chat, vision analysis, history, reminders, model status | n/a (per-message) |
| `ai_tools.py` | — | 18 LLM tool definitions + executors; all tools accept `camera` arg via `_resolve_camera()` helper | ✅ all 18 tools |
| `ai_prompts.py` | — | System prompt builder; injects camera registry list into LLM context | ✅ |
| `ai_state.py` | — | Shared AI state (DB refs, GPU flag, pending media) | n/a |
| `notifications.py` | `/api` | Telegram API helpers, scene analysis, snapshot drawing, `build_clip(camera_id=…)` | ✅ |
| `bot_commands.py` | — | Telegram polling loop, 15+ command handlers w/ `[camera]` token parsing, inline-keyboard camera picker, `/cameras` helper command | ✅ |
| `image_gen.py` | `/api/generate` | ComfyUI proxy, txt2img, img2img, gallery, prompt history | n/a (generation is global) |
| `recordings.py` | `/api/recordings` | DVR playback: `/dates`, `/segments`, `/stream/{date}/{segment}` all accept `?camera`. New `/cameras` endpoint lists every camera that has recordings on disk | ✅ |
| `events.py` | `/api/events` | Event feed; `?camera=` filters or `all`/empty merges streams newest-first; per-event `camera_id` field; `resolve_event_snapshot_path()` helper walks per-camera dirs w/ legacy-flat fallback | ✅ |
| `config.py` | `/api/config` | Per-camera config hash (`config:{camera_id}`); detection thresholds, notification toggles | ✅ |
| `zones.py` | `/api/zones` | Per-camera zone CRUD (`zones:{camera_id}` hash) | ✅ |
| `cameras.py` | `/api/cameras` | Camera registry: list/get/upsert/delete; `POST /test-rtsp` runs ffprobe; `GET /next-slot` for compose slot allocation | ✅ |
| `faces.py` | `/api/faces` | Face enrollment proxy to face-recognizer service (port 8081 not host-exposed) | shared DB |
| `unknowns.py` | `/api/unknowns` | Unknown face management (list, label, delete) | shared DB |
| `browse.py` | `/api/browse` | Vehicle snapshot browser; `/days`, `/days/{date}`, `/snapshot/{camera}/{date}/{filename}` (also `/{date}/{filename}` legacy form) | ✅ |
| `clips.py` | `/api/clips` | Video clip listing, serving, deletion | n/a |
| `conditions.py` | `/api/conditions` | Time period, sunrise/sunset, weather (global) | n/a (global) |
| `metrics.py` | `/api/metrics` | Prometheus metrics endpoint | per-camera (planned) |
| `auth.py` | `/api/auth` | Login, logout, session, password rotation | n/a |
| `telegram_access.py` | `/api/telegram` | Telegram user approval, role management, access log | n/a |
| `__init__.py` | — | Shared state (Redis clients, key names, defaults) | n/a |

**Known non-camera-aware endpoints (low priority):**
- `/api/login-bg` — login background image, always pulls from `frame_hd:{primary}`.

---

## Dashboard Frontend

### Pages

| File | URL | Purpose |
|------|-----|---------|
| `index.html` | `/` | **Multi-camera grid view** (default home since May 2026). Mobile-responsive tiles, click → modal. Below the grid: Conditions, **Recent Activity** (aggregate event feed across all cameras with per-row 📷 camera badge), and Known Faces panels. |
| `single.html` | `/single.html?camera=X` | Per-camera detailed dashboard. URL param scopes WebSocket + REST calls (config, zones, events, stats). Page title shows the camera's friendly name. `app.js` reads the param into `window.CAMERA_ID` and exposes `withCamera(url)` helper used by `zones.js`, `events.js`, etc. |
| `cameras.html` | `/cameras.html` | Camera registry admin: list/add/edit/delete cameras + Test Connection button (ffprobe) |
| `ai.html` | `/ai.html` | AI chat + vision + DVR + image generation |
| `monitoring.html` | `/monitoring.html` | System health + embedded Grafana |
| `telegram.html` | `/telegram.html` | Telegram user management + access log |
| `login.html` | `/login.html` | Authentication page |

### JavaScript Modules

| File | Lines | Purpose |
|------|-------|---------|
| `app.js` | 362 | WebSocket connection, settings sliders, module init |
| `ai.js` | 962 | AI chat, vision tab, DVR tab, onboarding wizard |
| `generate.js` | 1570 | Image generation, gallery, sweep, img2img, prompt history |
| `events.js` | 345 | Event feed polling, rendering, face cache, photo lightbox |
| `zones.js` | 500+ | Zone drawing canvas, CRUD operations, alert level config |
| `faces.js` | 430+ | Face enrollment wizard, multi-angle capture |
| `browse.js` | 260+ | Vehicle snapshot browser, face gallery |
| `conditions.js` | 200+ | Time period display, weather fetch |
| `unknowns.js` | 190+ | Unknown face grid, label/delete operations |
| `monitoring.js` | 180 | Health cards, Grafana iframe, fullscreen toggle |
| `telegram_access.js` | 240+ | User approval/revoke, access log viewer |
| `auth.js` | 110+ | Login form, session management |

### CSS

| File | Purpose |
|------|---------|
| `style.css` | Main dashboard styles (live view, events, settings, zones) |
| `ai.css` | AI chat interface, DVR player |
| `generate.css` | Image generation UI, gallery, sweep, lightbox |
| `monitoring.css` | System monitor cards, Grafana embed |

---

## Tracker Service

### Person Tracking
- **IoU matching**: for each new detection, compute overlap with every tracked person's last bbox
- **Threshold**: if IoU > 0.3, same person → update state
- **Debounce**: new person must persist 15 frames (~1s) before `person_appeared` event
- **Lost timeout**: if person not seen for `lost_timeout` seconds → `person_left` event
- **Action classification**: keypoint geometry → standing, sitting, crouching, lying (from `contracts/actions.py`)
- **Direction estimation**: bbox center history → left, right, stationary
- **Snapshot at detection**: tracker grabs the frame at event emission time (stored with 2h TTL)

### Vehicle Tracking
- Same IoU pattern, separate `TrackedVehicle` class
- **Idle detection**: if vehicle stationary > `vehicle_idle_timeout` (default 90s) → `vehicle_idle` event
- **Stationarity check**: max displacement from first center < 30px
- Vehicle snapshots stored with 24h TTL

### Zone Checks
- Each detection is checked against configured zones using ray-casting point-in-polygon
- Zone alert levels: `always`, `night_only`, `day_only`, `log_only`, `ignore`
- Alert decision uses `contracts/time_rules.py` `should_alert(zone_level, current_period)`
- Dead zones: detections inside dead zones are completely ignored

---

## Face Recognition

### Service: `face-recognizer`
- **Model**: InsightFace (buffalo_l) running on GPU
- **Database**: SQLite at `/data/faces.db`
- **Match threshold**: cosine similarity > 0.5
- **API port**: 8081 (proxied through dashboard)

### Endpoints (via dashboard proxy)
- `POST /api/faces/enroll` — capture frame, extract embedding, save to DB
- `GET /api/faces` — list all enrolled faces
- `GET /api/faces/{id}/photo` — serve portrait JPEG
- `DELETE /api/faces/{id}` — delete enrollment

### Sticky Identity
Once a face is recognized, the name stays on the bounding box even when the person turns away. A 10-frame vote buffer with 2× bias for the current identity prevents flicker.

### Unknown Face Management
Unrecognized faces are auto-captured and can be labeled later via the dashboard (`/api/unknowns`).

---

## AI Assistant

### Models
| Model | Purpose | Size |
|-------|---------|------|
| **Qwen 3 14B** | Chat + tool calling | ~9.3 GB |
| **MiniCPM-V** | Vision analysis (image description) | ~5 GB |

Both run via Ollama with 5-minute keep-alive. VRAM is shared with ComfyUI via a GPU lock flag.

### Chat Flow
```
User message → build system prompt with live context
→ Qwen 3 14B with TOOLS schema
→ if tool_calls: execute tool → feed result back → re-prompt (up to 5 rounds)
→ final text reply (with embedded media if tools produced any)
```

### System Context (injected each message)
- Current date/time, location, weather
- **Registered cameras list** (id=name, e.g. `cam1=front_door · cam2=basement`) so the LLM knows valid camera arg values
- People currently in frame across all cameras (from state keys)
- Known faces list
- Active zones (per camera)
- Recent events summary
- Notification status
- System health

### 18 Tool Functions (all multi-camera-aware)
See `routes/ai_tools.py` — each returns a JSON string. Tools that touch
per-camera data accept an optional `camera` arg:
- `""` or absent → primary camera (env `CAMERA_ID`)
- `"<id>"`        → specific camera, must exist in registry
- `"all"`         → aggregate across every enabled camera

Resolution via `_resolve_camera()` helper (ai_tools.py:66+). Per-camera Redis
keys built via `_camera_key()` from `contracts.streams` templates.

Multi-camera-aware tools: `query_events`, `query_events_by_date`,
`query_zones`, `query_event_patterns`, `query_activity_heatmap`,
`get_live_scene`, `get_system_status`, `capture_snapshot`, `capture_clip`,
`browse_vehicles`. Global tools (faces, weather, telegram, reminders,
schedule, notification history, show_faces, analyze_image) don't take
`camera` because the data is camera-agnostic.

Tools stash media (snapshots, clips, images) via `ai_state` for embedding in
the reply.

---

## Image Generation

### Service: ComfyUI
- Mounts `./models/comfyui/` for checkpoints, LoRAs, VAE
- Dashboard proxies all requests to `http://comfyui:8188`

### Features
- **txt2img**: prompt → ComfyUI workflow → poll for result
- **img2img**: upload source image + denoise strength
- **Batch generation**: queue multiple seeds
- **Parameter sweep**: steps × CFG × LoRA strength grid
- **Gallery**: browse generated images with metadata, lightbox preview, "Use These Settings"
- **Prompt history**: server-side storage with revision tracking (tracks changes during generation)
- **VRAM management**: `gpu:generation_active` flag pauses detectors during generation, auto-unloads Ollama models

---

## Notification System

### Alerts
When Telegram is configured, the dashboard background poller sends photo alerts:

| Event | Photo | Caption Contains |
|-------|:-----:|-----------------|
| `person_appeared` | HD snapshot with bbox | Time, zone, action, AI scene description |
| `person_identified` | HD snapshot with bbox | Name, time, zone |
| `vehicle_idle` | Vehicle snapshot with bbox | Vehicle class, duration, zone |

### Rate Limiting
- Per-event-type cooldowns (configurable via dashboard settings)
- Default: 60s person cooldown, 60s vehicle cooldown
- `suppress_known` toggle: skip notifications for recognized people

### Broadcasting
All alerts are sent to **every approved Telegram user** (multi-user support).

---

## Telegram Bot

### Polling Architecture
`bot_commands.py` runs a long-polling loop as a background task:
1. `getUpdates` from Telegram API (30s timeout)
2. Validate user via `telegram:users` Redis hash
3. Route to command handler
4. Log to `telegram:access_log` stream + per-user audit files on NAS

### Commands
`/snapshot`, `/clip [N]`, `/status`, `/arm`, `/disarm`, `/who`, `/events [N]`, `/analyze`, `/help`, plus photo analysis (send any photo to get MiniCPM-V description).

### Access Control
- Users managed via dashboard Telegram Access Manager page
- Roles: `admin` (full access) and `user` (limited)
- Bootstrap: `TELEGRAM_ALLOWED_USERS` env var seeds initial users

---

## DVR Recording

### Service: `recorder`
- **Method**: ffmpeg RTSP→MP4 copy (no transcode — very low CPU)
- **Segments**: 1-hour MP4 files
- **Retention**: 28-day rolling cleanup
- **Storage**: QNAP NAS via CIFS mount at `/recordings/`
- **Naming**: `{camera_id}/YYYY-MM-DD/HH-MM-SS.mp4`

### Playback API (`recordings.py`)
- `GET /api/recordings/dates` — list available recording dates
- `GET /api/recordings/segments?date=YYYY-MM-DD` — list segments for a date
- `GET /api/recordings/stream/{date}/{segment}` — stream MP4 with range support

---

## Zone System

### Zone Types
| Alert Level | Behavior |
|-------------|----------|
| `always` | Alert on any detection, any time |
| `night_only` | Alert only during night/late-night periods |
| `day_only` | Alert only during daytime |
| `log_only` | Log to event feed but no Telegram notification |
| `ignore` | Completely ignore detections (dead zone) |

### Time Periods
| Period | Window |
|--------|--------|
| **Daytime** | Sunrise + 30min → Sunset − 30min |
| **Twilight** | ±30 min around sunrise and sunset |
| **Night** | Sunset + 30min → Midnight |
| **Late Night** | Midnight → Sunrise − 30min |

### Zone Drawing
Browser-side canvas drawing tool with polygon support. Zones stored in `zones:{camera_id}` Redis hash, read by tracker for alert decisions and by dashboard for overlay rendering.

---

## Monitoring Stack

- **Prometheus** scrapes redis-exporter (Redis stats) and dcgm-exporter (GPU stats)
- **Grafana** serves dashboards at `:3000`, embedded in the System Monitor page via iframe
- **Dashboard metrics** (`routes/metrics.py`) exposes custom Prometheus metrics: inference timing, detection counts, event rates

---

## Shared Contracts

The `contracts/` directory is mounted read-only into every service container:

| File | Exports | Used By |
|------|---------|---------|
| `streams.py` | Redis key templates (`FRAME_STREAM`, `EVENT_STREAM`, etc.) + `stream_key()` resolver + data schema documentation | All services |
| `actions.py` | `classify_action(keypoints)` — keypoint geometry → action label | tracker |
| `time_rules.py` | `get_time_period(dt)`, `should_alert(level, period)`, `point_in_polygon()` | tracker, dashboard |

---

## Authentication

- **Session-based**: cookie + server-side session store in SQLite (`/data/auth.db`). Cookie name `vl_session`, signed HMAC-SHA256 of `username:timestamp`, 24h validity
- **Password hashing**: salted SHA-256 (`hashlib.sha256(f"{salt}:{password}").hexdigest()`)
- **Middleware**: `auth_middleware` in `server.py` intercepts all HTTP requests except login, static assets, and API auth endpoints
- **WebSocket auth**: `websocket_live` *also* validates the `vl_session` cookie and closes with code 4401 if invalid. HTTP middleware does not intercept WebSocket scopes, so the handler does this explicitly
- **Forced password change**: when admin still has the default password `admin`, the login endpoint returns `must_change_password: true`. `login.html` swaps to a forced-rotation form before letting the user reach the dashboard
- **Login page**: blurred camera snapshot background (no auth required for the blurred image itself)
- **Default seed user**: `admin/admin` is created on first DB initialization; the forced rotation flow above prevents anyone from actually staying on the default

---

## Storage Layout

All persistent state lives under `/data/` (and `/recordings/` for the recorder
services). Everything except recordings is in Docker-managed named volumes;
recordings are bind-mounted to `./data/recordings/` on the WSL host so they're
browsable from Windows Explorer without sudo.

```
./data/recordings/                                ← BIND MOUNT (./data/ on host)
└── {camera_id}/                                  ← e.g. front_door/, cam2/
    └── YYYY-MM-DD/
        └── HH-MM.ts                              ← MPEG-TS, ffmpeg copy (no transcode)
                                                  ← 1h segments, 3-day retention

/data/snapshots/                                  ← Docker volume qnap-snapshots
├── {camera_id}/                                  ← per-camera event snapshots
│   └── {event_id}.jpg                            ← {redis_stream_id}.jpg
├── vehicles/                                     ← vehicle snapshots
│   └── {camera_id}/                              ← per-camera (added by phase 9b iter 3)
│       └── YYYY-MM-DD/
│           └── HH-MM-SS_car.jpg
├── clips/                                        ← AI + Telegram /clip outputs
│   └── YYYYMMDD_HHMMSS_{camera}_{uuid}.mp4       ← 3-day retention
└── (legacy flat *.jpg pre-fan-out)               ← still readable via fallback

/data/events/                                     ← Docker volume qnap-events
└── YYYY-MM-DD.jsonl                              ← each entry has "camera" field
                                                  ← (added by phase 9b iter 2)

/data/telegram/                                   ← Docker volume qnap-telegram
└── {username_userid}/
    ├── commands.log
    └── media/

/data/generations/                                ← Docker volume qnap-generations
                                                  ← ComfyUI output images

/data/auth.db                                     ← Docker volume auth-data
                                                  ← SQLite: sessions, users, password rotation
```

**Future QNAP migration:** when a QNAP shows up, swap individual volumes
(snapshots, events, telegram, generations) from Docker-managed → NFS/CIFS
mounts pointing at the QNAP — no code changes needed. The recorder bind mount
becomes either a QNAP NFS mount or stays local with shorter retention.

**Retention** (defaults; configurable via env on the dashboard service):
- `SNAPSHOT_RETENTION_DAYS=4`  → person + vehicle event snapshots, event journal
- `CLIP_RETENTION_DAYS=3`       → AI/Telegram clips at `/data/snapshots/clips/`
- `RETENTION_DAYS=3` (recorder)  → continuous DVR segments per camera

---

## Docker Infrastructure

### Networking
- **camera-ingester** and **recorder**: `network_mode: host` (direct RTSP access to camera on LAN)
- **All other services**: Docker bridge network, communicate via DNS names (e.g., `redis`, `ollama`, `comfyui`)
- **Monitoring stack** (prometheus, grafana, redis-exporter, dcgm-exporter): `network_mode: host` for metric scraping

### Volumes
| Volume | Type | Purpose |
|--------|------|---------|
| `redis-data` | Docker | Redis AOF persistence |
| `face-data` | Docker | InsightFace SQLite DB + portraits |
| `yolo-models` | Docker | YOLO model weights cache |
| `insightface-models` | Docker | InsightFace model weights cache |
| `auth-data` | Docker | Auth SQLite DB |
| `ollama-models` | Docker | LLM model weights (~15 GB) |
| `comfyui-data` | Docker | ComfyUI output images |
| `prometheus-data` | Docker | Prometheus TSDB |
| `grafana-data` | Docker | Grafana state |
| `portainer-data` | Docker | Portainer state |
| `qnap-snapshots`, `qnap-events`, `qnap-telegram`, `qnap-generations`, `qnap-videos`, `qnap-clips` | Local Docker by default; CIFS when `docker-compose.qnap.yml` overlay is used | 6 storage volumes that can flip between local and NAS-backed at runtime |
| `./data/recordings` (bind mount) | Bind mount to WSL host path (since `ed2c3c2`) | DVR recordings — browseable from Windows Explorer without sudo; will be swapped to NFS mount when QNAP arrives, code paths unchanged |

### Profiles + Overlay Files

| Trigger | Effect |
|---------|--------|
| `docker compose up` (no profile) | Base file. Recorder runs (since `aa13d25`) with local bind mount + 3-day retention. The 6 `qnap-*` volumes are plain local Docker volumes |
| `docker compose --profile cam2 up` | Activates the cam2 slot: ingester, pose-detector, vehicle-detector, tracker, recorder all spin up for the basement camera |
| `docker compose -f docker-compose.yml -f docker-compose.qnap.yml up` | Overlay rewrites the 6 `qnap-*` volumes to CIFS mounts pointing at QNAP. Requires QNAP at `QNAP_IP` with a `vision-labs` share + 6 subfolders. Recordings stay local-disk by default until you swap that mount too |

### GPU Sharing — split across two cards
**GPU 0 (5070 Ti — always-on detectors):**
1. **pose-detector** — always running (~44ms/frame on Blackwell)
2. **vehicle-detector** — always running
3. **face-recognizer** — always running (honors `gpu:generation_active`)

**GPU 1 (3090 — on-demand heavy workloads):**
4. **ollama** — on-demand (5-min keep-alive), auto-unloaded during image generation
5. **comfyui** — on-demand, sets `gpu:generation_active` flag to pause the three detectors above

`dcgm-exporter` sees both GPUs (`count: all` reservation) for metrics.

---

## File Index

### Services
```
services/
├── camera-ingester/     # RTSP → Redis frames. Reads RTSP URLs from
│                        # cameras:registry when env vars are absent
│                        # (so slot-based cameras work).
├── pose-detector/       # YOLOv8s-pose → person bboxes. Checks
│                        # `detect_persons` flag from registry; exits cleanly if false.
├── vehicle-detector/    # YOLOv8s → vehicle bboxes. Checks `detect_vehicles`.
├── tracker/             # IoU tracking → semantic events.
├── face-recognizer/     # InsightFace → face identities. Checks `detect_faces`.
│                        # Shares the face DB across all cameras (Docker volume face-data).
├── dashboard/           # FastAPI backend + web frontend (refactored May 2026)
│   ├── server.py        # 353 lines — wiring only: imports, app, middleware, startup, static mount
│   ├── constants.py     # Ollama models, ComfyUI defaults — env-overridable
│   ├── websocket.py     # /ws/live; accepts ?camera=<id> query param for multi-cam
│   ├── cameras.py       # CameraRegistry (Redis-backed) + slot allocation
│   ├── ai_db.py         # AI chat history SQLite
│   ├── helpers/
│   │   └── geometry.py  # bbox_iou + in_dead_zone
│   ├── pollers/
│   │   ├── reminders.py        # 60s reminder dispatch via Telegram
│   │   ├── ollama_warmup.py    # Pulls chat model + warms GPU at startup
│   │   ├── comfyui_cleanup.py  # Clears stale GPU locks at startup
│   │   ├── retention.py        # Daily prune of /data/snapshots and /data/events
│   │   └── events.py           # Event stream consumer + Telegram broadcast + snapshot save
│   ├── routes/          # 21 API route modules (cameras.py was added; see Multi-camera section)
│   └── static/
│       ├── index.html   # NEW HOME: multi-camera grid view (mobile-responsive)
│       ├── grid.js      # Tile WebSocket + modal logic
│       ├── single.html  # Old per-camera dashboard (now at /single.html?camera=X)
│       ├── cameras.html # Camera registry admin UI
│       └── (existing JS/CSS modules — events.js, zones.js, faces.js, …)
├── recorder/            # ffmpeg RTSP → MP4 DVR (profile-gated to nas)
├── comfyui/             # Stable Diffusion inference
├── prometheus/          # Metrics config
└── grafana/             # Provisioned dashboard JSON
```

### Multi-camera state (Phase 7+)
- **Registry**: `cameras:registry` Redis hash — `{id: <camera_id>, name, rtsp_sub, rtsp_main, gpu_id, enabled, detect_persons, detect_vehicles, detect_faces}`.
- **Slot pattern**: each camera beyond `front_door` runs in a profile-gated set of services (`docker compose --profile cam2 up -d` starts ingester/pose/vehicle/face/tracker for cam2). The cam2 slot is in `docker-compose.yml`; cam3/cam4 are copy-paste additions.
- **WebSocket**: `/ws/live?camera=<id>` — each grid tile opens its own connection per camera.
- **AI tools** (Phase 9a iter 1): `get_live_scene` aggregates all cameras; `query_events` and `capture_snapshot` take a `camera` arg. System prompt enumerates registered cameras so the LLM can route. Remaining 7 tools still default to primary (iter 2 work).
- **Telegram commands**: still single-camera (iter 2/9b work).

### Contracts
```
contracts/
├── streams.py           # Redis key templates + data schemas (single source of truth)
├── actions.py           # Keypoint action classification
└── time_rules.py        # Time periods, zone alert rules, PIP test
```
