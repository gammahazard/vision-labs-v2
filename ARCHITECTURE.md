# Vision Labs — Architecture Reference

> ℹ️ **Scope and conventions.** This document explains the *architectural reasoning*
> behind Vision Labs — why services are split this way, why Redis Streams is the bus,
> why GPU placement matters, etc. The structural picture is current as of 2026-05-20.
>
> For day-to-day operational details (AI tool catalog, env-var inventory, current
> Telegram alert types, retention defaults), see **[CONTEXT.md](CONTEXT.md)** which
> tracks closer to the running code.
>
> **Sample camera ids throughout this doc are `cam1`, `cam2`, etc.** All slots are
> symmetric — there is no privileged "primary" camera in the runtime, though
> `CAMERA_ID` env var resolves to `cam1` by default for non-camera-scoped tools.
> Older drafts of this doc used the slot name `front_door`; that was renamed
> to `cam1` in the Phase G refactor (May 18) along with a one-shot data
> migration of 104 Redis keys, 718 events, and 1012 identities.

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
│                    │                                                  │
│            Ollama (GPU 1)                                            │
│            Qwen 3 14B + MiniCPM-V                                    │
│                                                                      │
│  Recorder ──▶ ./data/recordings/{cam}/YYYY-MM-DD/HH-MM.ts (bind)     │
│  Prometheus + Grafana + Portainer ──▶ Ops UIs                        │
└──────────────────────────────────────────────────────────────────────┘
```

**GPU split rationale:** the three always-on detectors total <4 GB VRAM and fit comfortably on the 5070 Ti. Ollama is the heavy on-demand workload that benefits from the 3090's 24 GB headroom.

---

## Service Map

| Service | Container | Host port | GPU | Description |
|---------|-----------|-----------|:---:|-------------|
| **redis** | redis:7-alpine | 127.0.0.1:6379 | — | Central message bus, AOF persistence, 2GB maxmemory. Bound to localhost only since 2026-05-19 — LAN devices can't reach Redis even if `REDIS_PASSWORD` is empty. Inside-network services use the `redis` hostname; host-network services connect via localhost |
| **camera-ingester** | custom | host net | — | RTSP→JPEG frames, publishes sub-stream + HD to Redis. Hot-reloads `target_fps` from Redis config |
| **pose-detector** | custom | — | GPU 0 | YOLOv8s-pose, publishes person bboxes + keypoints |
| **vehicle-detector** | custom | — | GPU 0 | YOLOv8s, publishes vehicle bboxes |
| **tracker** | custom | — | — | IoU-based person/vehicle tracking, event publishing |
| **face-recognizer** | custom | *internal only (8081 not exposed)* | GPU 0 | InsightFace embedding, SQLite DB, REST API proxied through dashboard |
| **vehicle-attributes** | custom | — | GPU 0 | Per-cam HD-crop buffer; flushes per-track dir on track end. Phase 3 v0 ConvNeXt-Tiny classifier (two models: color + body/make/model) lazy-loaded from HF Hub, gated by `ENABLE_CLASSIFIER`. Profile-gated per slot |
| **dashboard** | custom | 8080 | — | FastAPI + WebSocket + static files + background tasks. WebSocket `/ws/live` requires session cookie |
| **ollama** | ollama/ollama | 11434 | GPU 1 | LLM inference (Qwen 3 14B, MiniCPM-V) |
| **recorder** | custom | host net | — | RTSP→`.ts` ffmpeg copy, 1-hour segments, 3-day retention. Runs by default to `./data/recordings/` (bind mount). All `recorder-camN` (cam1..cam20) are profile-gated to their slot; `recorder.py` reads RTSP URL from `cameras:registry` if `RTSP_URL` env is empty (same fallback as the ingester) |
| **orchestrator** | custom | — | — | Reconciles compose profiles against `cameras:registry`. Subscribes to `cameras:events` pub/sub channel + runs a periodic safety-net reconcile. Has the Docker socket; the dashboard does NOT. Writes every action to `orchestrator:audit` Redis stream. See Phase 7b decision in docs/history/REFACTOR_PLAN.md |
| **prometheus** | prom/prometheus | 127.0.0.1:9090 (host net) | — | Metrics collection, 30d retention. `--web.listen-address=127.0.0.1:9090` so the admin API isn't reachable from the LAN |
| **grafana** | grafana/grafana-oss | 3000 (host net) | — | Monitoring dashboards, provisioned from `services/grafana/dashboards/vision-labs.json` |
| **redis-exporter** | redis_exporter | host net | — | Redis metrics → Prometheus |
| **dcgm-exporter** | dcgm-exporter | host net | all | NVIDIA GPU metrics → Prometheus |
| **portainer** | portainer/portainer-ce | 9000 (HTTP), 9443 (HTTPS) | — | Docker management UI |

---

## Data Flow Pipelines

### Detection Pipeline (real-time, ~5-10 FPS — limited by camera sub-stream rate)

```
1. camera-ingester:
   - OpenCV reads RTSP sub-stream at TARGET_FPS (default 15 env, hot-reloaded from Redis config:cam1.target_fps)
   - Forces TCP transport (OPENCV_FFMPEG_CAPTURE_OPTIONS set BEFORE cv2 import)
   - JPEG encode each frame at JPEG_QUALITY (default 75)
   - XADD to frames:cam1 (capped at MAX_STREAM_LEN=1000)
   - Separate thread: reads main stream at HD_TARGET_FPS (default 8),
     SETEX frame_hd:cam1 with 5s TTL

2. pose-detector (consumer group "pose_detectors"):
   - XREADGROUP from frames stream
   - YOLOv8s-pose inference (~44ms on RTX 5070 Ti)
   - Filter: person class only, confidence > threshold
   - SET detection_frame:pose:cam1 = the frame JPEG used
   - XADD detections:pose:cam1 {detections: JSON, inference_ms}

3. vehicle-detector (consumer group "vehicle_detectors"):
   - Same pattern as pose-detector
   - Classes: car, truck, bus, motorcycle
   - Embeds `frame_bytes` directly in detection messages (load-bearing for tracker
     vehicle snapshots — captures the exact frame the bbox was computed from)
   - SET detection_frame:vehicle:cam1 = frame used
   - XADD detections:vehicle:cam1

4. tracker (consumer groups "trackers" + "vehicle_trackers"):
   - XREADGROUP from detections:pose + detections:vehicle
   - IoU matching against tracked persons/vehicles
   - If new person: debounce 15 frames, then emit person_appeared
   - If person gone > lost_timeout: emit person_left
   - If face recognized: emit person_identified
   - If vehicle stationary > vehicle_idle_timeout: emit vehicle_idle
   - HSET state:cam1 with current scene snapshot
   - XADD events:cam1 {event_type, person_id, bbox, zone, ...}
   - SETEX person_snapshot:{camera}:{ts} = frame JPEG (2h TTL)
   - SETEX vehicle_snapshot:{camera}:{ts} = frame JPEG (24h TTL)

5. face-recognizer (consumer group "face_recognizers"):
   - XREADGROUP from detections:pose stream (only runs face matching on frames where pose detected a person)
   - XREVRANGE frames stream to grab the matching frame
   - InsightFace detection + embedding extraction (CUDA via onnxruntime-gpu)
   - Compare against SQLite DB of enrolled faces (cosine similarity)
   - HSET identity_state:cam1 with matches + EXPIRE 5 s (refreshed on every successful write; empty-detection frames are skipped so the key expires naturally, avoiding a race with tracker's 2 s identity poll)
   - XADD identities:cam1

6. dashboard WebSocket (/ws/live — authenticated, validates vl_session cookie):
   - Read latest frame from detection_frame:pose:cam1
   - Read state:cam1 for current persons/vehicles
   - Read identity_state:cam1 for face labels
   - Read zones:cam1 for zone overlays
   - Draw bounding boxes, keypoints, face labels, zone overlays
   - JPEG encode at q=85 → base64 → send JSON via WebSocket
   - Render rate hot-reloaded from config:cam1.target_fps (default 10)
```

### Event Notification Flow

```
1. tracker emits event to events:cam1
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
| `orchestrator:audit` | orchestrator | dashboard (camera status badges) | `{action, profile, success, detail, timestamp}` — last 500 actions taken by the orchestrator (up / down / no-op) |

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
| `telegram:users` | dashboard (Telegram Access Manager) | bot_commands | `{user_id: JSON{chat_id, name, role, approved_at}}` |
| `telegram:last_offset` | bot_commands | bot_commands | Persisted Telegram update offset — restored at startup so restarts don't replay old commands |
| `cameras:registry` | dashboard (cameras page) | every service (per-camera config + detector flags) | Hash keyed by camera_id; JSON value with `id`, `name`, `rtsp_sub`, `rtsp_main`, `enabled`, `detect_persons`, `detect_vehicles`, `detect_faces`, `gpu_id`, location, timestamps |
| `cameras:events` *(pub/sub channel)* | dashboard cameras CRUD | orchestrator | Tiny JSON payload `{action, camera_id, ts}` that nudges the orchestrator the registry changed; orchestrator always reconciles against `cameras:registry` regardless of payload contents |
| `notify:last` | notifications | notifications | Hash of `{event_type: unix_ts}` — last-sent timestamps for cooldown gating; persisted so a dashboard restart doesn't bypass active cooldowns |

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

### `server.py` (~460 lines)

The main FastAPI app is a **thin wiring file**. The 1300-line original (May 2026 refactor) was split into 6 modules:

| Component | Lives in | Purpose |
|-----------|----------|---------|
| `auth_middleware` | server.py:~200 | Session-based auth, redirects (303) unauthenticated to login |
| `login_background` | server.py:~240 | Heavily blurred camera snapshot for login page (no auth) |
| `startup` event | server.py:~270 | Init auth DB, write default config, seed camera registry, schedule pollers |
| `reminder_poller` | `pollers/reminders.py` | Check due reminders every 60s, send via Telegram |
| `warm_ollama` | `pollers/ollama_warmup.py` | Pull Qwen 3 14B on first startup, warm-up GPU load |
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
| `ai_tools/` (package) | — | 19 LLM tools, one file per tool + `_shared.py` + `__init__.py` dispatcher. All tools accept `camera` arg via `_resolve_camera()` helper. Split from a 2264-line monolith on 2026-05-19 | ✅ all 19 tools |
| `ai_prompts.py` | — | System prompt builder; injects camera registry list into LLM context | ✅ |
| `ai_state.py` | — | Shared AI state (DB refs, GPU flag, pending media) | n/a |
| `notifications/` (package) | `/api` | Split into `endpoints.py` (router), `telegram_api.py` (send/broadcast wrappers + 429 retry), `frame.py` (snapshot helpers + `build_clip`), `scene.py` (MiniCPM-V describe_scene + prompts), `alerts.py` (notify_person/identified/vehicle/face_enrolled), `_shared.py` (config + cooldowns + auth gate). Package since 2026-05-19. | ✅ |
| `bot_commands/` (package) | — | `_poller.py` (long-poll loop), `_dispatch.py` (17-command router), one file per command. Package since 2026-05-19 | ✅ |
| `recordings.py` | `/api/recordings` | DVR playback: `/dates`, `/segments`, `/stream/{date}/{segment}` all accept `?camera`. New `/cameras` endpoint lists every camera that has recordings on disk | ✅ |
| `events.py` | `/api/events` | Event feed; `?camera=` filters or `all`/empty merges streams newest-first; per-event `camera_id` field; `resolve_event_snapshot_path()` helper walks per-camera dirs w/ legacy-flat fallback | ✅ |
| `config.py` | `/api/config` | Per-camera config hash (`config:{camera_id}`); detection thresholds, notification toggles | ✅ |
| `zones.py` | `/api/zones` | Per-camera zone CRUD (`zones:{camera_id}` hash) | ✅ |
| `cameras.py` | `/api/cameras` | Camera registry: list / get / upsert / delete; `POST /test-rtsp` (ffprobe); `GET /next-slot` (compose slot allocation); `GET /{id}/status` (latest `orchestrator:audit` entry → live status badge); `PATCH /{id}/enabled` (pause/unpause without delete). Upsert + delete publish to `cameras:events` so the orchestrator reconciles immediately | ✅ |
| `faces.py` | `/api/faces` | Face enrollment proxy to face-recognizer service (port 8081 not host-exposed) | shared DB |
| `unknowns.py` | `/api/unknowns` | Unknown face management (list, label, delete) | shared DB |
| `browse.py` | `/api/browse` | Vehicle snapshot browser; `/days`, `/days/{date}`, `/snapshot/{camera}/{date}/{filename}` (also `/{date}/{filename}` legacy form) | ✅ |
| `containers.py` | `/api/containers` | Docker container state + uptime listing (read-only; the orchestrator owns the socket, dashboard just reads names + Up/Exited via `orchestrator:audit`) | n/a |
| `setup.py` | `/api/setup` | First-run wizard endpoints — GPU probe, ONVIF camera discovery, config-apply | n/a |
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
| `setup.html` | `/setup.html` | First-run wizard: hardware probe → tier recommendation → location/retention → ONVIF camera discovery → Telegram pairing. Auto-shown until `/data/setup-state/setup.json` exists (middleware gate in `server.py`). |
| `login.html` | `/login.html` | Authentication page |

### JavaScript Modules

| File | Lines | Purpose |
|------|-------|---------|
| `app.js` | ~410 | WebSocket connection, settings sliders, module init |
| `ai.js` + `pages/ai/_*.js` | 167 entry + 6 siblings (state, utils, wizard, chat, vision-tab, dvr-tab; ~1290 total) | AI chat, vision tab, DVR tab, onboarding wizard. Split 2026-05-22 — see §13. |
| `events.js` | ~820 | Event feed polling + cursor pagination + journal fallback, type-filter pills, name search, Face Matched detail modal (thumbnail grid + similarity scores), photo lightbox |
| `zones.js` | ~530 | Zone drawing canvas, CRUD operations, alert level config |
| `faces.js` | ~570 | Face enrollment wizard, multi-angle capture |
| `browse.js` | ~460 | Vehicle snapshot browser, face gallery, per-track crops modal |
| `conditions.js` | ~200 | Time period display, weather fetch |
| `unknowns.js` | ~190 | Unknown face grid, label/delete operations |
| `monitoring.js` | ~280 | Health cards, Grafana iframe, fullscreen toggle |
| `telegram_access.js` | ~240 | User approval/revoke, access log viewer |
| `auth.js` | ~110 | Login form, session management |

### CSS

| File | Purpose |
|------|---------|
| `style.css` + `css/components/*.css` | Main dashboard styles. 2026-05-22: the original 2747-line `style.css` became a 32-line `@import` entry-point plus 13 focused component files under `css/components/` (navbar, layout, live-view, settings, events, faces, zones, conditions, account, browse, feedback, responsive, _base). |
| `ai.css` | AI chat interface, DVR player |
| `monitoring.css` | System monitor cards, Grafana embed |

---

## Tracker Service

The `PersonTracker` class (despite the name, owns both person and vehicle tracking) lives in `services/tracker/core/manager.py`. The 1365-line monolith was split via **mixin classes** on 2026-05-22:

| File | Lines | Owns |
|---|---|---|
| `manager.py` | ~660 | PersonTracker class + `__init__`/`_generate_id`/`_process_vehicle_detections`/`_update_state`/`update`. Keeps env-driven sample-throttle constants here so `importlib.reload(manager)` still picks them up in tests. |
| `_vehicle_matcher.py` | ~290 | `VehicleMatcherMixin` — fallback strategies (idle-IoM rescue, ghost rescue, live center-distance, ghost center-distance) + sample-quality gates (`_bbox_area`, `_sample_occluded_by_moving_vehicle`) |
| `_vehicle_events.py` | ~210 | `VehicleEventsMixin` — five emit_vehicle_* event writers + per-sample HD-snapshot writes |
| `_person_events.py` | ~125 | `PersonEventsMixin` — person event emit + snapshot pairing |
| `_zones.py` | ~80 | `ZonesMixin` — zone polygon load + lookup + dead-zone gate |
| `_identity.py` | ~115 | `IdentityMixin` — face-recognizer → track identity sync with N-cycle flip guard |
| `_classes.py` | 28 | `_class_compatible` helper for YOLO car↔truck↔bus flicker tolerance |

`PersonTracker(VehicleMatcherMixin, VehicleEventsMixin, PersonEventsMixin, ZonesMixin, IdentityMixin)` — mixins keep `self.r` / `self.tracked_vehicles` / `self._ghost_vehicles` etc. accessible without threading them through call signatures.

### Person Tracking
- **IoU matching**: for each new detection, compute overlap with every tracked person's last bbox
- **Threshold**: if IoU > 0.3, same person → update state. Identified tracks use a looser `IDENTITY_TRACK_IOU_THRESHOLD` (default 0.10) so a known person walking far away keeps their track id + identity instead of getting destroyed and re-spawned as Unknown.
- **Debounce**: new person must persist 15 frames (~1s) before `person_appeared` event
- **Lost timeout**: if person not seen for `lost_timeout` seconds → `person_left` event. Identified tracks use `IDENTITY_LOST_TIMEOUT` (default 30 s) so an identified person walking off + back within ~30 s keeps their track id.
- **Identity demotion**: on a > `IDENTITY_PERSIST_GAP_SECS` (default 6 s) silent gap with no face-recognizer confirmation, identity_name is cleared on the next re-match — prevents a stranger inheriting an identified track's bbox spot.
- **Action classification**: keypoint geometry → standing, walking, sitting, crouching, lying, arms_raised (from `contracts/actions.py`). `MIN_KEYPOINTS_FOR_ACTION` gates partial detections.
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

Both run via Ollama with 5-minute keep-alive.

### Chat Flow
```
User message → build system prompt with live context
→ Qwen 3 14B with TOOLS schema
→ if tool_calls: execute tool → feed result back → re-prompt (up to 5 rounds)
→ final text reply (with embedded media if tools produced any)
```

### System Context (injected each message)
- Current date/time, location, weather
- **Registered cameras list** (id=name, e.g. `cam1=cam1 · cam2=basement`) so the LLM knows valid camera arg values
- People currently in frame across all cameras (from state keys)
- Known faces list
- Active zones (per camera)
- Recent events summary
- Notification status
- System health

### 19 Tool Functions (all multi-camera-aware)
See `routes/ai_tools/` — one file per tool, each returns a JSON string.
Tools that touch per-camera data accept an optional `camera` arg:
- `""` or absent → primary camera (env `CAMERA_ID`)
- `"<id>"`        → specific camera, must exist in registry
- `"all"`         → aggregate across every enabled camera

Resolution via `_resolve_camera()` helper in `routes/ai_tools/_shared.py`.
Per-camera Redis keys built via `_camera_key()` from `contracts.streams`
templates.

Multi-camera-aware tools: `query_events`, `query_events_by_date`,
`query_zones`, `query_event_patterns`, `query_activity_heatmap`,
`get_live_scene`, `get_system_status`, `capture_snapshot`, `capture_clip`,
`browse_vehicles`. Global tools (faces, weather, telegram, reminders,
schedule, notification history, show_faces, analyze_image) don't take
`camera` because the data is camera-agnostic.

Tools stash media (snapshots, clips, images) via `ai_state` for embedding in
the reply.

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
`routes/bot_commands/_poller.py` runs a long-polling loop as a background task:
1. `getUpdates` from Telegram API (30s timeout)
2. Validate user via `telegram:users` Redis hash
3. Route to command handler
4. Log to `telegram:access_log` stream + per-user audit files on NAS

### Commands (16 total, one file per command under `routes/bot_commands/`)
**User:** `/snapshot [cam]`, `/clip [Ns] [cam]`, `/status [cam]`, `/who [cam]`, `/events [N] [cam]`, `/zones [cam]`, `/timelapse [YYYY-MM-DD] [cam]`, `/analyze [cam] [prompt]`, `/ask <question>`, `/rules`, `/night`, `/faces`, `/cameras`, `/start`, `/help`.

**Admin only (role=admin in `telegram:users`):** `/arm`, `/disarm`.

Plus a photo handler that runs MiniCPM-V on any incoming image. Camera token parsing accepts id (`cam2`), friendly name (`basement`), unambiguous prefix (`base`), or `all`.

### Access Control
- Users managed via dashboard Telegram Access Manager page
- Roles: `admin` (full access) and `user` (limited)
- Bootstrap: `TELEGRAM_ALLOWED_USERS` env var seeds initial users

---

## DVR Recording

### Service: `recorder-camN` (one per slot, all profile-gated)
- **Method**: ffmpeg RTSP → MPEG-TS copy (no transcode — very low CPU)
- **Segments**: 1-hour `.ts` files
- **Retention**: 3-day rolling cleanup (`RETENTION_DAYS=3`)
- **Storage**: bind-mounted to `./data/recordings/` on the WSL host (browseable from Windows Explorer without sudo)
- **Naming**: `{camera_id}/YYYY-MM-DD/HH-MM.ts`
- **QNAP overlay** (optional): when `docker-compose.qnap.yml` is layered in, the bind mount can be swapped to an NFS/CIFS path with no code change

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

- **Stateless sessions** — no server-side store. The `vl_session` cookie is a self-contained HMAC-SHA256-signed token: `username:must_change_flag:timestamp:signature`. SQLite (`/data/auth.db`) only holds the user table + the HMAC secret key.
- **Password hashing**: bcrypt (cost 12). Legacy salted SHA-256 hashes are still accepted on login for upgrades from pre-2026-05 installs; on a successful SHA-256 verify, `_maybe_upgrade_to_bcrypt` rewrites the row with a fresh bcrypt hash. No batch migration, no downtime.
- **Middleware**: `auth_middleware` in `server.py` intercepts all HTTP requests except a small `_AUTH_EXEMPT` set (login page, login API, blurred-bg, base CSS + auth.js, favicon, metrics).
- **WebSocket auth**: `websocket_live` *also* validates the `vl_session` cookie and closes with code 4401 if invalid. HTTP middleware does not intercept WebSocket scopes, so the handler does this explicitly (after `ws.accept()`, which is required to send a close frame).
- **Default-credentials gate (server-enforced)**: when the user logs in with `admin/admin`, the session token gets `must_change=1` baked in. The middleware refuses every route except `/api/auth/change-password` (and assets the login page needs) until the user rotates. A curl client that ignores the UI's rotation prompt still hits the 403. After a successful rotation, the change-password endpoint issues a new token with `must_change=0`.
- **Password rules**: minimum 8 characters; the literal string `"admin"` is explicitly rejected on change-password.
- **Brute-force gate** on `/api/auth/login`: in-memory per-IP counter. 5 failed attempts in 5 minutes → 15-minute lockout, returning HTTP 429 with `Retry-After`. Successful login clears the counter for that IP. Resets on container restart (acceptable for single-host LAN install).
- **Login page**: blurred camera snapshot background (no auth required for the blurred image itself).
- **Default seed user**: `admin/admin` is created on first DB initialization. The server-enforced rotation gate prevents staying on the default.

---

## Storage Layout

All persistent state lives under `/data/` (and `/recordings/` for the recorder
services). Everything except recordings is in Docker-managed named volumes;
recordings are bind-mounted to `./data/recordings/` on the WSL host so they're
browsable from Windows Explorer without sudo.

```
./data/recordings/                                ← BIND MOUNT (./data/ on host)
└── {camera_id}/                                  ← e.g. cam1/, cam2/
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

/data/auth.db                                     ← Docker volume auth-data
                                                  ← SQLite: users table (username, bcrypt hash)
                                                  ← + app_config (HMAC secret_key persisted
                                                  ←   for stable cookie signing across restarts)
                                                  ← Sessions are NOT stored — tokens are HMAC-signed
                                                  ←   self-contained `username:flag:ts:sig`.
/data/ai.db                                       ← Docker volume auth-data (shares the volume)
                                                  ← SQLite: ai_config (single row),
                                                  ←   reminders (Telegram timed messages),
                                                  ←   chat_history (last N messages, server-side)
```

**Future QNAP migration:** when a QNAP shows up, swap individual volumes
(snapshots, events, telegram) from Docker-managed → NFS/CIFS
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
- **All other services**: Docker bridge network, communicate via DNS names (e.g., `redis`, `ollama`)
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
| `prometheus-data` | Docker | Prometheus TSDB |
| `grafana-data` | Docker | Grafana state |
| `portainer-data` | Docker | Portainer state |
| `qnap-snapshots`, `qnap-events`, `qnap-telegram`, `qnap-videos`, `qnap-clips` | Local Docker by default; CIFS when `docker-compose.qnap.yml` overlay is used | 5 storage volumes that can flip between local and NAS-backed at runtime |
| `./data/recordings` (bind mount) | Bind mount to WSL host path (since `ed2c3c2`) | DVR recordings — browseable from Windows Explorer without sudo; will be swapped to NFS mount when QNAP arrives, code paths unchanged |
| `./services/dashboard/static` + `./services/dashboard/{routes,pollers,helpers,*.py}` | Read-only bind mounts on the dashboard container | Dashboard source live-reload. HTML/CSS/JS changes are live on browser refresh; Python changes only need `docker compose restart dashboard` (no rebuild). The image still `COPY`s them for builds-from-scratch — the mounts just shadow that at runtime |
| `/var/run/docker.sock` → `/var/run/docker.sock` (on orchestrator only) | Bind mount | Required for the orchestrator to manage compose profiles. **Deliberately NOT mounted on the dashboard** — see Phase 7b decision in docs/history/REFACTOR_PLAN.md |

### Profiles + Overlay Files

| Trigger | Effect |
|---------|--------|
| `docker compose up` (no profile) | Base file. The 6 `qnap-*` volumes are plain local Docker volumes. The `orchestrator` service starts here and is responsible for activating cam1..cam20 profiles based on the `cameras:registry` Redis hash |
| `docker compose --profile camN up` (manual) | Manual activation of a slot — normally NOT needed since the orchestrator does this automatically when a camera is registered in the dashboard. Useful for debugging or first-run before the orchestrator is in place |
| `docker compose -f docker-compose.yml -f docker-compose.qnap.yml up` | Overlay rewrites the 6 `qnap-*` volumes to CIFS mounts pointing at QNAP. Requires QNAP at `QNAP_IP` with a `vision-labs` share + 6 subfolders. Recordings stay local-disk by default until you swap that mount too |

### GPU Sharing — split across two cards
**GPU 0 (5070 Ti — always-on detectors):**
1. **pose-detector** — always running (~44ms/frame on Blackwell)
2. **vehicle-detector** — always running
3. **face-recognizer** — always running

**GPU 1 (3090 — on-demand):**
4. **ollama** — on-demand (5-min keep-alive)

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
│   ├── server.py        # ~460 lines — wiring only: imports, app, middleware, startup, static mount
│   ├── constants.py     # Ollama models — env-overridable
│   ├── websocket.py     # /ws/live; accepts ?camera=<id> query param for multi-cam
│   ├── cameras.py       # CameraRegistry (Redis-backed) + slot allocation
│   ├── ai_db.py         # AI chat history SQLite
│   ├── helpers/
│   │   └── geometry.py  # bbox_iou + in_dead_zone
│   ├── pollers/
│   │   ├── reminders.py        # 60s reminder dispatch via Telegram
│   │   ├── ollama_warmup.py    # Pulls chat model + warms GPU at startup
│   │   ├── retention.py        # Daily prune of /data/snapshots and /data/events
│   │   └── events.py           # Event stream consumer + Telegram broadcast + snapshot save
│   ├── routes/          # ~16 route modules + 3 packages (ai_tools/, bot_commands/, notifications/)
│   └── static/
│       ├── index.html   # HOME: multi-camera grid view (mobile-responsive)
│       ├── single.html  # Per-camera dashboard (/single.html?camera=X)
│       ├── cameras.html # Camera registry admin UI
│       ├── ai.html / monitoring.html / telegram.html / setup.html / login.html
│       ├── css/         # style.css (shared base) + ai/monitoring/setup page-specific
│       ├── js/core/     # nav.js, auth.js — loaded on every authenticated page
│       ├── js/dashboard/# app, grid, events, faces, unknowns, zones, browse,
│       │                # conditions, settings — the camera-view feature cluster
│       └── js/pages/    # ai, cameras, monitoring, setup, telegram — 1:1 with HTML
                          # (Reorganized from a flat layout on 2026-05-19.)
├── recorder/            # ffmpeg RTSP → .ts DVR. All recorder-camN (cam1..cam20) are
│                        # profile-gated and managed by the orchestrator. recorder.py
│                        # reads RTSP URL from cameras:registry if RTSP_URL env is empty.
├── orchestrator/        # NEW (Phase 7b). Single-purpose sidecar with the Docker socket.
│                        # Watches cameras:events + cameras:registry; runs
│                        # `docker compose --profile <cam> up/down` automatically.
│                        # Writes audit history to orchestrator:audit Redis stream.
├── prometheus/          # Metrics config
└── grafana/             # Provisioned dashboard JSON
```

### Multi-camera state (Phase 7+ — fully shipped)
- **Registry**: `cameras:registry` Redis hash — `{id, name, rtsp_sub, rtsp_main, gpu_id, enabled, detect_persons, detect_vehicles, detect_faces}`.
- **Slot pool (Phase 7c → expanded 2026-05-19)**: pre-defined service blocks for `cam1` through `cam20` in `docker-compose.yml`, each profile-gated. After Phase G all slots are symmetric — no privileged primary. (Originally 5 slots, then 10, now 20. **Future:** dynamic slot generation via orchestrator-written compose override would remove the cap.)
- **Auto-orchestration (Phase 7b)**: the `orchestrator` service watches `cameras:registry` + the `cameras:events` pub/sub channel and reconciles compose profiles automatically — adding a camera in the dashboard spawns its services within ~10s, no terminal command needed. Writes every action to `orchestrator:audit` so the UI can show live status badges. The dashboard intentionally does NOT have the Docker socket; the orchestrator does, with a strict allowlist of profiles it may up/down (`ALLOWED_PROFILES`).
- **Per-camera detector flags**: each detector reads `detect_persons` / `detect_vehicles` / `detect_faces` from its registry entry at startup and exits cleanly (Exit 0, restart policy `on-failure`) if its detector is disabled. Saves GPU.
- **WebSocket**: `/ws/live?camera=<id>` — each grid tile opens its own connection per camera.
- **AI tools**: every multi-camera-relevant tool accepts a `camera` arg (`""` / `"<id>"` / `"all"`). See `routes/ai_tools/` (package).
- **Telegram commands**: per-command `[camera]` token parsing — one command per file under `routes/bot_commands/`.

### Contracts
```
contracts/
├── streams.py           # Redis key templates + data schemas (single source of truth)
├── actions.py           # Keypoint action classification
└── time_rules.py        # Time periods, zone alert rules, PIP test
```

---

## Recent additions (post-2026-05-18)

Beyond the structural picture above, the following operationally significant additions
have shipped. These are documented here so a reader of the architecture doc doesn't
have to discover them in the code:

**Stream-health events (B1)** — `camera-ingester` emits `stream_stale` and
`stream_recovered` events on `events:{cam}` when the RTSP feed dies (no decoded
frame for ≥30s) and resumes. The dashboard's event poller routes both through
Telegram automatically. Single emit per stale period via a module-level flag.

**Recorder-health events (B2)** — `recorder` emits `recorder_error` after 3
consecutive ffmpeg sessions <30s each (sustained crash) and `recorder_recovered`
when a session runs ≥5 min. Same Telegram path as B1.

**Resource pressure poller (B3)** — `services/dashboard/pollers/health.py` runs
every 60s, alerts when `/data` disk or Redis memory crosses 85% (hysteresis clears
at 75%). Bypasses per-event-type config gating — these alerts always fire.

**JSONL event-journal fallback (C1)** — `query_events_by_date` merges Redis
xrange with `/data/events/<date>.jsonl`. When the events stream hits its
`MAX_EVENT_STREAM_LEN` cap (default 5000) and trims older entries, the journal
fills the gap. Dedup by event_id; Redis wins on collisions.

**bcrypt migration (C2)** — `routes/auth.py` accepts both bcrypt (`$2b$…`) and
legacy salted SHA-256 hashes on login. After a successful SHA-256 verify,
`_maybe_upgrade_to_bcrypt` rewrites the row with a fresh bcrypt hash. No batch
migration, no downtime.

**DHCP resilience (D2)** — On RTSP connect failure, `camera-ingester` re-reads
`cameras:registry` via `_refresh_rtsp_from_registry`. If the URL changed (admin
edit OR wizard update reflecting a new DHCP lease), reconnect immediately with
the new value instead of looping on the stale one.

**LLM tool-data attach (D1)** — `/api/ai/chat` now returns `tool_calls: [{name,
args, result}]` alongside `reply`. The frontend renders a collapsible "show
tool data" toggle under each assistant message that used tools, so the user
can verify counts/identities against the raw tool output. Mitigation for
Qwen 3 14B hallucinations on aggregate queries.

For day-to-day operational details on AI tools, see [CONTEXT.md §9](CONTEXT.md).
For the full audit + remediation log, see docs/history/PHASES.md.
