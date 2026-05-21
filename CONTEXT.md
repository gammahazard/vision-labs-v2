# Vision Labs — Full Project Context

> One-stop reference for a new session (human or LLM) to understand the project end-to-end. Last updated 2026-05-20.

This document is the canonical context for working in this repo. It assumes you can read source code, but tells you **where to look**, **what reads what**, and **what to be careful of**. For architectural reasoning ("why services are split this way") see ARCHITECTURE.md — this doc tracks operational details (current AI tool catalog, env vars, retention defaults). Historical planning docs live in `docs/history/`.

---

## 1. What Vision Labs is

A self-hosted, GPU-accelerated, multi-camera AI security stack. Runs on a single host via Docker Compose. No cloud dependencies for the inference pipeline.

**Inputs:** RTSP camera streams (sub for inference, main for HD viewing/recording).

**Outputs:**
- Live multi-camera grid dashboard (browser) with overlaid person/vehicle bboxes, names, zones, action labels
- Real-time event stream + JSONL journal
- Telegram alerts with AI-generated scene descriptions and on-demand commands
- Persistent face enrollment DB shared across all cameras
- 1-hour MPEG-TS DVR segments with rolling retention
- Local LLM chat assistant (Qwen 3 14B) with 19 callable tools

**Hardware target:** NVIDIA GPU (CUDA 12.8+ for Blackwell support). Default config is single-GPU; dual-GPU split via `DETECTOR_GPU` / `CHAT_GPU` env vars. Tested on dual-card workstation (5070 Ti + 3090) running Ubuntu 24.04 under WSL2 on Windows. macOS not supported.

**Communication backbone:** Redis (Streams + pub/sub + key-value). Every service-to-service hop goes through Redis — there are very few direct HTTP calls (face-recognizer's REST API for enrollment is the main one).

---

## 2. High-level architecture

```
RTSP camera
    │ (sub-stream + main-stream)
    ▼
camera-ingester-camN ── XADD frames:camN ──┐
        │                                  │ (JPEG bytes per frame)
        │                                  ▼
        │                       ┌──────────────────────┐
        │                       │  pose-detector-camN  │ ──▶ XADD detections:pose:camN
        │                       └──────────────────────┘            │
        │                       ┌──────────────────────┐            │
        │                       │ vehicle-detector-camN│ ──▶ XADD detections:vehicle:camN
        │                       └──────────────────────┘            │
        │                       ┌──────────────────────┐            │
        │                       │face-recognizer-camN  │ ◀──────────┤ (cropping faces from pose bboxes)
        │                       └──────────────────────┘            │   XADD identities:camN
        │                                                           │   HSET identity_state:camN
        ▼                                                           ▼
SETEX frame_hd:camN (5s)                                ┌────────────────┐
        │                                               │  tracker-camN  │ ──▶ XADD events:camN
        │                                               └────────────────┘     HSET state:camN
        │                                                                      SETEX person_snapshot:camN:*
        │                                                                      SETEX vehicle_snapshot:camN:*
        ▼                                                           ▼
┌────────────────────────────────────────────────────────────────────────────────┐
│                                  dashboard                                      │
│   FastAPI + WebSocket + REST + background pollers + Telegram dispatch + AI      │
└────────────────────────────────────────────────────────────────────────────────┘
        │                                       │
        ▼                                       ▼
    browser                                Telegram bot
                                                ▼
                                          ollama (Qwen 3 14B chat + MiniCPM-V vision)

orchestrator (separate container, owns Docker socket) ◀── subscribes to cameras:events
                                                          publishes orchestrator:audit
                                                          runs `docker compose --profile camN up/down`

recorder-camN (host-net ffmpeg) ──▶ ./data/recordings/camN/YYYY-MM-DD/HH-MM.ts (bind mount)

prometheus + grafana + redis-exporter + dcgm-exporter ──▶ embedded monitoring page

portainer ──▶ https://localhost:9443 (Docker management UI)
```

---

## 3. Camera slot model — `cam1` through `cam20`

**This was refactored from a special `front_door` primary on 2026-05-18 (Phase G).** All slots are now symmetric. There is no longer a privileged primary camera. Slots cam1–cam5 originally; cam6–cam10 added 2026-05-19; cam11–cam20 added 2026-05-19. The cap is a docker-compose authoring artifact (each slot is a templated block) — not an architectural limit. **Future work:** the orchestrator could generate slot blocks dynamically via a `docker-compose.override.yml` it writes itself, removing the cap entirely. Not done yet.

- `AVAILABLE_SLOTS = [f"cam{n}" for n in range(1, 21)]` (services/dashboard/cameras.py:82)
- Each slot has 7 profile-gated services in docker-compose.yml: `camera-ingester-camN`, `pose-detector-camN`, `vehicle-detector-camN`, `face-recognizer-camN`, `tracker-camN`, `recorder-camN`, `vehicle-attributes-camN`.
- `profiles: ["camN"]` on every block. None of the per-cam services start with bare `docker compose up`; the orchestrator brings them up.
- Adding a camera in the UI = upsert into `cameras:registry` + publish `cameras:events`. The orchestrator's reconcile loop sees the new slot and runs `docker compose --profile camN up -d <services>`.
- Removing/disabling a camera = orchestrator runs `compose stop` + `compose rm -f -s` on just that slot's services (never bare `compose down` — that tears down everything).
- `next-slot` endpoint (cameras.py) walks `AVAILABLE_SLOTS` in order, so the wizard always proposes cam1 first.

**Why this matters:** When you see `front_door` in older docs or git history, that's the legacy primary. It's gone. Migration scripts exist in `scripts/migrate-front-door-to-cam1.sh` and `scripts/migrate-stream-fields.sh` and have already been run on this dev host. The only places `front_door` still legitimately appears: those migration scripts, and historical planning docs under `docs/history/`.

---

## 4. Service inventory

All paths under `services/`. Every service ID below is profile-gated unless noted.

### 4.1 `base/` — shared CUDA image
- **Dockerfile:** `FROM nvidia/cuda:12.8.0-cudnn-runtime-ubuntu22.04`. Adds Python 3.11, OpenCV system libs, pip: `redis==5.2.1`, `numpy<2`, `opencv-python-headless==4.10.0.84`.
- **Not run directly.** pose/vehicle/face Dockerfiles do `FROM vision-labs-base:cuda12.8`.
- **Must be built before `docker compose up`** — `scripts/build.sh` builds this first, then everything else.

### 4.2 `camera-ingester/` (per-cam, host-net)
- **Reads:** RTSP via `RTSP_URL` env (only cam1 has `${CAM1_RTSP_URL}` populated; cam2..cam20 fall back to `cameras:registry`).
- **Writes:** `frames:{camN}` Redis stream (maxlen=`MAX_STREAM_LEN`=1000) + `frame_hd:{camN}` key (5s TTL) for HD live view.
- **Hot-reloads** `config:{camN}.target_fps` every 25 frames.
- **Gotcha:** sets `os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"]="rtsp_transport;tcp"` *before* importing cv2 (ingester.py:44). RTSP-over-TCP avoids packet loss on busy LANs.

### 4.3 `pose-detector/` (per-cam, GPU)
- **YOLOv8s-pose** by default (`POSE_MODEL=/models/yolov8s-pose.pt`; nano `yolov8n-pose.pt` for small tier).
- **Reads:** `frames:{camN}` via consumer group `pose_detectors`. Registry flag `detect_persons` gates startup (clean exit if false).
- **Writes:** `detections:pose:{camN}` stream (embeds `frame_bytes` in entries that have detections — paired with the bboxes, so the tracker's person-snapshot path + face-recognizer's face-crop path both work against the exact frame the bbox was computed from, no temporal drift) + `detection_frame:pose:{camN}` key (the exact JPEG inferred on, used by the WebSocket to align bboxes — see gotcha §14.4).
- **Hot-reloads** `config:{camN}` for confidence/min_keypoints every 25 frames.

### 4.4 `vehicle-detector/` (per-cam, GPU)
- YOLOv8s, filters COCO classes `[2,3,5,7]` = car/motorcycle/bus/truck.
- **Reads:** `frames:{camN}` (consumer group `vehicle_detectors`). Throttles via `FRAME_SKIP=3`. Drops bboxes < `MIN_VEHICLE_BBOX_AREA=2500 px²`. Gated by registry `detect_vehicles`.
- **Writes:** `detections:vehicle:{camN}` (embedded `frame_bytes` in entry when there are vehicles, so tracker can save the snapshot without re-fetching the frame).

### 4.5 `face-recognizer/` (per-cam, GPU, REST API)
- **InsightFace `buffalo_l`** model, `onnxruntime-gpu` (no PyTorch — saves ~5 GB image size).
- **Reads:** `detections:pose:{camN}` (consumer group `face_recognizers`). Crops face region from person bbox using the `frame_bytes` shipped on the same detection message — same frame the bbox was computed from, no temporal drift. Falls back to `xrevrange(frames:{camN}, count=1)` if `frame_bytes` is missing (defensive path for rolling upgrades where face-rec is ahead of the pose-detector). Registry flag `detect_faces`, which has a hard dependency on `detect_persons=true` — enforced server-side in `cameras.py:_validate_camera` and surfaced client-side via the generic `data-requires=` checkbox pattern in `js/lib/checkbox-dependencies.js` (wired into both `setup.html` and `cameras.html`).
- **Writes:** `identities:{camN}` stream + `identity_state:{camN}` hash. Emits `face_enrolled` / `face_reconciled` to `events:{camN}` on enrollment/labeling/reconcile.
- **Storage:** SQLite at `/data/faces.db` (volume `face-data`, mounted by ALL face-recognizer-camN containers — they share the DB). Tables: `known_faces` (id/name/embedding/photo BLOB/created_at), `unknown_faces` (id/embedding/photo/first_seen/last_seen/sighting_count).
- **HTTP (port 8081, NOT exposed to host)** — accessed only by the dashboard via internal Docker DNS `face-recognizer-cam1:8081`:
  - `GET /api/faces`, `GET /api/faces/{id}/photo`, `POST /api/faces/preview`, `POST /api/faces/enroll`, `DELETE /api/faces/{id}`
  - `GET /api/unknowns`, `GET /api/unknowns/{uid}/photo`, `POST /api/unknowns/{uid}/label`, `DELETE /api/unknowns/{uid}`, `DELETE /api/unknowns`, `POST /api/unknowns/scan`
- **Caches:** atomic-swap `_load_cache()` reload every `CACHE_REFRESH_INTERVAL=30s`. Lets two face-recognizer containers stay in sync on shared SQLite without lock contention.
- **Reconcile tiers** (used both on save and on retroactive sweep) — `match_threshold` default 0.5:
  - `sim ≥ match_threshold` → promote unknown to known under matched name (adds as extra angle).
  - `match_threshold * 0.6 ≤ sim < match_threshold` → delete unknown (loose match noise).
  - `sim < match_threshold * 0.6` → keep as unknown.

### 4.6 `vehicle-attributes/` (per-cam, no GPU in Phase 1)
- **Per-track HD-crop buffer.** Consumes `events:{cam}` filtered to `vehicle_detected` / `vehicle_sample` / `vehicle_gone` / `vehicle_idle`. Maintains in-memory `dict[track_id, TrackBuffer]`; cap 8 crops per track (`MAX_BUFFER_CROPS` env, default 8).
- **HD frame source:** `GET frame_hd:{cam}` (binary Redis client, 5 s TTL set by camera-ingester). On miss, skip the sample — next event tries again.
- **Cropping pipeline:** sub→HD bbox scale (×2.571 / ×2.531 at 896×512→2304×1296) → 20% padding (`CROP_PADDING_PCT`) → `cv2.imdecode` → slice → `cv2.imencode` JPEG q=85 (`JPEG_QUALITY`).
- **Storage:** flushes to `/data/snapshots/vehicles/{cam}/{date}/{track_id}/{hero.jpg, angle_NN.jpg, metadata.json}` on `vehicle_gone` (internal track-end event, fires for ALL track ends — drive-by + idle-leave) or `vehicle_idle` (preview flush mid-life). Hero = highest-confidence crop. Empty buffer = silent no-op. *Why not `vehicle_left`?* That's now strictly idle-leave-only (user-facing event); drive-by tracks would never flush if we used it.
- **Registry gate:** exits cleanly at startup if `detect_vehicles=false` OR `detect_vehicle_attributes!=true` (mirrors face-recognizer's pattern). `restart: on-failure` on the compose block leaves clean exits alone (vs `unless-stopped` which would loop).
- **No GPU in Phase 1** — Dockerfile uses `python:3.11-slim`, no PyTorch/ONNX. Phase 3 will add the multi-head classifier per the spec at `docs/superpowers/specs/2026-05-21-vehicle-attribute-classification-design.md`. The all-null `attributes` block in metadata.json is committed now so Phase 3 only fills values, doesn't restructure.
- **Producer/consumer:** consumes `events:{cam}` + `frame_hd:{cam}` + `cameras:registry`. Writes filesystem only (no Redis writes in Phase 1).
- **Tracker integration:** tracker emits `vehicle_sample` event every `SAMPLE_INTERVAL_FRAMES` matched updates when `EMIT_VEHICLE_SAMPLES=1` (default 0). Tracker code at `services/tracker/core/manager.py:_emit_vehicle_sample_event`.

### 4.7 `tracker/` (per-cam, no GPU)
- **Reads:** `detections:pose:{camN}` (consumer group `trackers`) + `detections:vehicle:{camN}` (consumer group `vehicle_trackers`). Plus `identity_state:{camN}` (every 2s), `zones:{camN}` (cached 10s), `config:{camN}` (every 10 messages).
- **Writes:** `events:{camN}` stream (maxlen 5000) — events `person_appeared`, `person_left`, `person_identified`, `action_changed`, `vehicle_detected`, `vehicle_idle`. Updates `state:{camN}` hash with `num_people` + `people` JSON.
- **Snapshot persistence on event:**
  - `person_snapshot:{camN}:{ts}` JPEG + `:bbox` JSON companion — 2h TTL.
  - `vehicle_snapshot:{camN}:{ts}` JPEG + `:bbox` JSON — 24h TTL.
- **Logic highlights:**
  - IoU matching against tracked persons (default 0.3).
  - Action classification via `contracts/actions.py` with debounce 10 frames + 2× sticky multiplier on the return path.
  - Identity sticky — once `TrackedPerson.identity_name` is set, only overwritten by non-empty new value.
  - Identity grace period — when `suppress_known=1` config, defers `person_appeared` for 4s; if face-recognizer identifies the person within the window, the event is suppressed entirely.
  - Vehicle idle detection requires ≥5 center-history samples AND the current center stays within `max(20 px, bbox_w * 0.15)` of the **median** of the rolling 20-sample center history (per `TrackedVehicle.is_stationary` in `services/tracker/core/state.py`), AND no prior `idle_alerted` flag. The bbox-scaled threshold replaced an earlier absolute 30 px cap so distant/small vehicles don't oscillate the flag. The 20 px floor / 15% width was bumped from `8 px / 10% width` after cam1 live data showed YOLO bbox jitter (~5-8 px frame-to-frame on a parked car) flipping `is_stationary` False on single noisy frames, which reset `idle_alerted` and re-emitted `vehicle_idle` every few minutes.

### 4.8 `recorder/` (per-cam, host-net)
- ffmpeg `-c copy -f segment` (no transcode) into `/recordings/{camN}/YYYY-MM-DD/HH-MM.ts` (1-hour MPEG-TS segments).
- **Bind-mount target:** `./data/recordings/` on the host (browseable from Windows Explorer via `\\wsl$\<distro>\...`).
- **RTSP URL resolution:** `RTSP_URL` env first; falls back to `cameras:registry[CAMERA_ID].rtsp_sub` — `_load_rtsp_from_registry()`.
- **Retention:** `cleanup_old_recordings()` deletes day-folders older than `RETENTION_DAYS=3` every 6 hours (`CLEANUP_INTERVAL`).
- **Host network** required to reach LAN RTSP URLs.

### 4.9 `dashboard/` — see §6 for full breakdown.
- FastAPI + Jinja-free static frontend + WebSocket + REST + background asyncio pollers.
- Port 8080 (the only user-facing port besides Grafana/Prom/Portainer).
- Always-on (NOT profile-gated). Single instance regardless of camera count.

### 4.10 `orchestrator/`
- Owns the Docker socket. Tiny image (alpine docker:24-cli + py3-redis).
- **4 background threads** (each on its own redis.Redis connection):
  1. Main event listener — subscribes `cameras:events` (registry mutations) → full reconcile.
  2. Probe listener — subscribes `setup:probe-request` → spawn `nvidia-smi` container → push result to `setup:probe-result` stream.
  3. Config-apply listener — subscribes `config:apply` → `compose up -d --force-recreate --no-deps <services>`.
  4. Reconcile loop (10s) — safety net.
- **Reconcile algorithm:**
  - `desired = enabled_cameras ∩ ALLOWED_PROFILES`
  - `running = docker compose ps --services --filter status=running` matched against `-{slot}` suffix
  - `to_start = desired - running` → `compose --profile X up -d --no-recreate <services>` (180s timeout)
  - `to_stop = running - desired` → `compose stop <services>` (90s) then `compose rm -f -s <services>` (30s)
- **Audit stream** `orchestrator:audit` (maxlen 500) with `{action, profile, success, detail, timestamp}`.
- **`ALLOWED_PROFILES=cam1,cam2,...,cam20`** strict allowlist — refuses any other profile name. See docker-compose.yml's orchestrator block for the canonical comma-separated list.
- **Critical safety rules (orchestrator.py):**
  - NEVER `compose --profile X down` (tears down ALL services).
  - NEVER `--remove-orphans` (deletes out-of-profile services).
  - Always pass an explicit `<services>` list filtered by `-{slot}` suffix.

### 4.11 `prometheus/`, `grafana/`, `redis-exporter`, `dcgm-exporter`, `node-exporter`
- All host-network. Scrapes via `localhost:9100/9121/9400/8080`.
- **Prometheus** bound to `127.0.0.1:9090` only (so LAN can't hit the admin API). `--storage.tsdb.retention.time=30d` + `--storage.tsdb.retention.size=5GB`. `--web.enable-admin-api` enabled for `scripts/prometheus-clean-stale-cameras.sh`.
- **Grafana**: provisioning + dashboards bind-mounted RO; port 3000; admin/visionlabs; anonymous viewer enabled so the monitoring page can embed without sign-in. Host-bound (LAN-reachable) as a trade-off for the iframe embed. Dashboard auto-refresh = 15s.
- **redis-exporter**: standard Redis stats on 9121.
- **dcgm-exporter**: GPU metrics with `gpu` (index) and `modelName` labels; Grafana labels use both to produce "GPU0 util (RTX 5070 Ti)" style legends.
- **node-exporter**: host disk / memory / CPU / network on `127.0.0.1:9100`. Mounts `/proc`, `/sys`, and `/` (no rslave on WSL2). The dashboard's Grafana "Host" row uses this for disk-usage gauge, free-space trendline, CPU and memory.

### 4.12 `portainer/`
- Web UI at `https://localhost:9443`. Optional but pre-installed for users who want a GUI on top of Docker. **Portainer ships with `Content-Security-Policy: frame-ancestors 'none'`** so it can NOT be iframe-embedded; the monitoring page's "Containers" tab is a read-only listing fed by the orchestrator (see §6 routes/containers), with an "↗ Open Portainer" button that opens it in a new tab.

---

## 5. Contracts (`contracts/`)

Bind-mounted RO into every service at `/app/contracts`. The single source of truth for stream/key names and shared algorithms.

> **Build/runtime split — the #1 footgun.** Only `contracts/` is bind-mounted at runtime; every other service's `.py` file is COPY'd into its Docker image at build time. That means editing `services/<svc>/<svc>.py` does **not** take effect in a running container — you must `docker compose build <svc>` first, then recreate. Editing anything under `contracts/` is hot — just restart the service. The orchestrator pulls `contracts/` via `/workspace` (the project mount), not `/app/contracts`, because its image is intentionally tiny (alpine + docker-cli).
>
> This split bit us in May 2026 when we added Redis AUTH. The dashboard worked (recently rebuilt with `make_redis_client`) but every detector failed because their old images still had hardcoded `redis.Redis(host=..., port=...)` calls. Always rebuild affected service images after touching their `.py`.

### 5.1 `contracts/streams.py` — every stream/key template

| Template | Type | Written by | Read by |
|---|---|---|---|
| `frames:{camera_id}` | stream (maxlen 1000) | camera-ingester | pose-detector, vehicle-detector, face-recognizer (face crop), dashboard WS |
| `detections:pose:{camera_id}` | stream | pose-detector | tracker, face-recognizer |
| `detections:vehicle:{camera_id}` | stream | vehicle-detector | tracker, metrics |
| `events:{camera_id}` | stream (maxlen 5000) | tracker, face-recognizer (face_enrolled/_reconciled), bot_commands (unauthorized_access) | dashboard pollers, routes/events.py |
| `state:{camera_id}` | hash | tracker | WebSocket, metrics, /who Telegram cmd |
| `identities:{camera_id}` | stream | face-recognizer | (logging only) |
| `identity_state:{camera_id}` | hash | face-recognizer | tracker `_update_identities`, WebSocket sticky-label |
| `config:{camera_id}` | hash | dashboard routes/config.py | ingester, pose/vehicle/face/tracker, WebSocket, notifications |
| `zones:{camera_id}` | hash | dashboard routes/zones.py | tracker `_load_zones`, WebSocket overlay |
| `frame_hd:{camera_id}` | string (5s TTL) | ingester HD thread | dashboard HD view, notifications, login-bg |
| `detection_frame:pose:{camera_id}` | string | pose-detector | WebSocket (bbox alignment) |
| `detection_frame:vehicle:{camera_id}` | string | vehicle-detector | WebSocket |
| `person_snapshot:{camera_id}:{ts}` (+ `:bbox`) | string (2h TTL) | tracker | dashboard events, Telegram |
| `vehicle_snapshot:{camera_id}:{ts}` (+ `:bbox`) | string (24h TTL) | tracker | dashboard browse, vehicle endpoints |
| `cameras:registry` | hash | dashboard routes/cameras.py | every per-cam service, orchestrator |
| `cameras:events` | pub/sub | dashboard | orchestrator |
| `setup:probe-request` | pub/sub | dashboard routes/setup.py | orchestrator |
| `setup:probe-result` | stream (maxlen 50) | orchestrator | dashboard routes/setup.py |
| `config:apply` | pub/sub | dashboard routes/setup.py | orchestrator |
| `orchestrator:audit` | stream (maxlen 500) | orchestrator | dashboard routes/cameras.py status badge |
| `notify:last` | hash | dashboard `routes/notifications/_shared.py` | itself (cooldown persistence) |
| `scene_analysis:{event_id}` | string (24h TTL) | dashboard `routes/notifications/scene.py` (describe_scene) | routes/events.py /analysis endpoint |
| `telegram:users` | hash | dashboard routes/telegram_access.py | notifications._is_authorized |
| `telegram:access_log` | stream (maxlen 500) | dashboard bot_commands | telegram.html access-log view |
| `telegram:last_offset` | string | dashboard bot_commands | bot_commands startup |

Dataclasses (`FrameMessage`, `DetectionMessage`, `EventMessage`) at the bottom of streams.py are **documentation only** — services use raw dicts. Field name mismatches are noted in comments (wire format uses `frame`, dataclass names it `frame_bytes`).

### 5.2 `contracts/actions.py`
- 17 COCO keypoint indices defined at top (`NOSE=0`, `L_SHOULDER=5`, …, `R_ANKLE=16`).
- `classify_action(keypoints, bbox=None) → {action, confidence, details}`. Resolution order (after the 2026 refactor that fixed false-sitting on basement cams):
  - `arms_raised` — either wrist y < shoulder y minus `max(8 px, body_scale * ARMS_RAISED_RATIO[=0.10])`; confidence 0.8.
  - `lying_down` — torso x-span > torso y-span × 1.5 AND y-span < `body_scale * LYING_TORSO_VERT_RATIO[=0.20]`; confidence 0.7.
  - **`sitting` — moved BEFORE crouching.** Requires knees at hip level AND either (ankles visible below knees) for 0.75 confidence, or (ankles hidden + tight 40% torso check) for 0.6. Torso-only branch was REMOVED — that was the false-sitting bug on cameras with cropped feet.
  - `crouching` — knee angle < `CROUCH_KNEE_ANGLE_DEG[=100]` (was 120; tightened so a chair-sitter at ~90° doesn't trip this branch when the sitting check skipped); confidence 0.7.
  - `standing` (default; 0.6).
- All pixel thresholds scale by `_body_scale()` (shoulder-hip vertical distance or bbox×0.40 fallback or 30 px floor) so the classifier works at any frame resolution / distance.
- `MIN_KP_CONF = 0.3`. Defensive: short keypoint arrays are zero-padded to 17 entries so partial inputs don't crash.
- Only used by tracker.

### 5.3 `contracts/time_rules.py`
- Periods: `daytime` (sunrise+30m → sunset−30m), `twilight` (±30m around either), `night` (sunset+30m → midnight), `late_night` (midnight → sunrise−30m).
- Astral library for sunrise/sunset; falls back to hardcoded 7am/7pm if astral missing.
- `LOCATION_LAT/LON/TIMEZONE/NAME/REGION` env. Default Toronto.
- Alert levels: `always`, `night_only`, `log_only`, `ignore`, `dead_zone`.
- `should_alert(level, period)` + `point_in_polygon(px, py, polygon)` used by tracker and dashboard zone overlay.

---

## 6. Dashboard internals (`services/dashboard/`)

This is the biggest service. Read this section when working on UI, routes, or background tasks.

### 6.1 `server.py` startup sequence (lines 299–360)
1. `init_auth_db()` — creates `/data/auth.db`, default admin/admin user, HMAC secret key (env override `SECRET_KEY`).
2. Default `config:{camN}` seeding for any registered cam with empty config hash.
3. `auto_mark_complete_if_preexisting()` — writes setup.json automatically if registry already has cameras (upgraded install).
4. AI DB init (`/data/ai.db`).
5. Background asyncio tasks: `event_notification_poller`, `poll_telegram_callbacks`, `reminder_poller`, `warm_ollama`, `retention_poller`, `health_poller` (disk + Redis memory alerts), `start_metrics_collector`.
6. WebSocket registered via `register_websocket(app)`.
7. StaticFiles mounted AFTER routers (so `/api/*` wins over static `index.html`).
8. **auth_middleware** (line 204) validates `vl_session` cookie via `validate_session`. Redirects to `/login.html` (HTML routes) or returns 401 (API routes).
9. **`_setup_exempt()`** gate (line 250) — if setup.json missing, only `/setup.html`, `/js/pages/setup.js`, `/css/setup.css`, `/api/setup/*`, `/api/auth/*`, `/static/*`, `/api/cameras*` are accessible. (After the 2026-05-19 static-folder reorg, JS/CSS now live under `/js/<group>/` and `/css/` subdirs respectively.)

### 6.2 `routes/` — 18 router modules + 3 split packages

The packages (`ai_tools/`, `bot_commands/`, `notifications/`) are former monoliths refactored on 2026-05-19; their public surface (router object, function names) is unchanged.

Prefix shown in parens. Most routes auth-gated by middleware (not per-endpoint dep).

- **`auth.py`** (`/api/auth`) — `POST /login` (returns `must_change_password: true` for default admin/admin), `POST /logout`, `POST /change-password` (allows username rename), `GET /status`.
- **`setup.py`** (`/api/setup`) — `GET /status`, `POST /detect-hardware` (pub/sub round-trip to orchestrator, 60s wait), `POST /discover-cameras` (wraps cameras.discover_cameras), `POST /apply-config` (env_writer.update_env + pub/sub config:apply), `POST /complete` (atomic write of setup.json).
- **`cameras.py`** (`/api/cameras`) — `POST /test-rtsp`, `POST /discover` (ONVIF), `POST /onvif-stream-uri` (SOAP GetProfiles+GetStreamUri with WSSE digest), `GET ""` (list), **`GET /next-slot`** (MUST be before `/{camera_id}` due to FastAPI route ordering), `GET /{camera_id}`, `POST ""` (upsert, validates against AVAILABLE_SLOTS, returns 400 for unknown slot), `PUT /{camera_id}`, `DELETE /{camera_id}`, `GET /{camera_id}/status` (last orchestrator audit), `PATCH /{camera_id}/enabled`.
- **`config.py`** (`/api`) — `GET /config?camera=`, `POST /config?camera=` (writes allowed keys), `GET /stats?camera=`.
- **`conditions.py`** (`/api`) — `GET /conditions` (sunrise/sunset + weather, cached 15min).
- **`events.py`** (`/api`) — `GET /events?count&camera&before` (merges per-cam streams + falls through to JSONL when Redis exhausted), `GET /events/{id}/snapshot`, `GET /events/{id}/analysis`, `GET /vehicles/snapshot/{key:path}` (with bbox draw).
- **`faces.py`** (`/api`) — 5 endpoints, all proxy to `FACE_API_URL`. Enroll also calls `notify_face_enrolled`.
- **`unknowns.py`** (`/api`) — 6 endpoints proxying face-recognizer. `/label` pre-fetches photo (since labeling deletes the unknown) and stashes it as `person_snapshot:{cam}:{ts}` so the emitted `person_identified` event has a thumbnail.
- **`zones.py`** (`/api`) — per-camera CRUD via `?camera=`. Alert level enum validation.
- **`browse.py`** (`/api/browse`) — `GET /days`, `GET /days/{date}`, `GET /snapshot/{cam_or_legacy}/{date}/{file}`, legacy `GET /snapshot/{date}/{file}`, `GET /faces`.
- **`notifications/`** (`/api`, package as of 2026-05-19) — `POST /notifications/test` and `GET /notifications/status` live in `endpoints.py`. Internal Telegram dispatch is split across `_shared.py`, `telegram_api.py`, `frame.py`, `scene.py`, `alerts.py`. Old `routes/notifications.py` was 1102 lines; package surface is unchanged (see §8).
- **`recordings.py`** — `GET /api/recordings/cameras`, `GET /api/recordings/dates?camera=`, `GET /api/recordings/segments?date&camera`, `GET /api/recordings/stream/{date}/{segment}?camera=` (remux .ts→.mp4 via async ffmpeg, cached in `/tmp/rec-cache` with 5 GB LRU eviction).
- **`containers.py`** — `GET /api/containers` returns a snapshot of every project container (name/service/state/status/health). Reads `orchestrator:containers` from Redis — the orchestrator publishes that key every reconcile (60 s TTL). Read-only by design: the dashboard never touches the Docker socket; for start/stop/exec/logs the UI links out to Portainer.
- **`metrics.py`** — `GET /metrics` (Prom text exposition), `GET /api/monitoring/health`. Counters/gauges all labeled by `camera`: `vl_detections_total`, `vl_vehicle_detections_total`, `vl_events_total`, `vl_active_persons`, `vl_inference_ms`, `vl_frames_per_second`, `vl_stream_length`, `vl_notifications_total`. Background collector polls every 10s. Per-camera last-seen-id dicts (`_last_*_id_by_cam`) — single global was a bug that broke multi-cam metrics.
- **`ai.py`** (`/api/ai`) — chat (Qwen 3 14B with tool calls), history, vision (MiniCPM-V), clip serving, config, reset, reminders. See §9.
- **`ai_state.py`** — per-request `request_id → media` stash under threading.Lock. Lets web + Telegram `/ask` run concurrently without stealing each other's snapshots.
- **`ai_tools/`** — 19 LLM tools (one file per tool + `_shared.py` + `__init__.py` dispatcher). Package as of 2026-05-19. See §9.
- **`ai_prompts.py`** — `build_system_context` (live Redis snapshot — cameras, faces, zone count, event stream length) + `build_system_prompt`.
- **`bot_commands/`** — Package as of 2026-05-19. `_poller.py` is the long-poll loop, `_dispatch.py` is the command router (17 commands wired), one file per command, plus `_shared.py` and `analyze.py` for photo-message handling. See §8.
- **`telegram_access.py`** (`/api/telegram`) — `GET /users`, `POST /users` (approve), `DELETE /users/{uid}`, `GET /access-log?count=` (XREVRANGE, capped 200), `DELETE /access-log`.

### 6.3 `helpers/`
- **`env_writer.py`** — `update_env(updates, path)` with strict allowlist (`ALLOWED_KEYS`):
  - GPU/model: `DETECTOR_GPU`, `CHAT_GPU`, `CHAT_MODEL`, `VISION_MODEL`, `POSE_MODEL`, `VEHICLE_MODEL`, `TARGET_FPS`
  - Location + retention: `LOCATION_TIMEZONE`, `LOCATION_NAME`, `LOCATION_REGION`, `LOCATION_LAT`, `LOCATION_LON`, `SNAPSHOT_RETENTION_DAYS`, `CLIP_RETENTION_DAYS`, `RETENTION_DAYS`
  - Telegram (wizard-settable): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_ALLOWED_USERS`

  Strategy: `tempfile.mkstemp` + `os.replace`; **on EBUSY (errno 16) falls back to direct truncate-write** (because Docker bind-mounted files reject rename onto the mount target).
- **`geometry.py`** — `bbox_iou(a, b)`, `in_dead_zone(bbox, w, h, zone_cache)`. Used by WebSocket.
- **`onvif_discovery.py`** — Unicast WS-Discovery (NOT multicast — multicast silently fails on WSL2). ThreadPool of 50 workers, 2s per-IP, 15s overall deadline. Safety cap 4096 hosts. `detect_local_cidr()` priority chain: (1) cameras:registry RTSP URL → host extraction, (2) `CAMERA_IP` env, (3) RTSP env vars, (4) UDP-socket trick to 8.8.8.8 — filters out Docker bridge ranges (172.17–172.31).

### 6.4 `pollers/`
- **`events.py`** — `event_notification_poller` reads each registered camera's events stream, dispatches Telegram + saves snapshots to `/data/snapshots/{cam}/{event_id}.jpg` + appends to `/data/events/YYYY-MM-DD.jsonl`.
- **`ollama_warmup.py`** — `warm_ollama()` pulls the chat model if missing, sends a real warm-up chat to force it into VRAM, signals `set_gpu_ready_flag(True)`. Also pulls `VISION_MODEL` (MiniCPM-V) via `_ensure_vision_model()` so first Telegram alert with auto-description doesn't fail — added 2026-05-20.
- **`reminders.py`** — `reminder_poller(_ai_db)` walks `reminders` table in ai.db, fires Telegram messages when due.
- **`retention.py`** — Daily prune of `/data/snapshots` and `/data/events` (default 4 days; `SNAPSHOT_RETENTION_DAYS=0` disables).
- **`health.py`** — Watches disk free space + Redis memory; emits a Telegram alert when either crosses configured thresholds (cooldowns per resource so a stuck condition doesn't spam).

### 6.5 `static/` — HTML pages + JS (reorganized 2026-05-19)

```
static/
├── *.html               (8 pages at root: index, single, ai, cameras,
│                         monitoring, telegram, setup, login)
├── favicon.svg
├── css/                 (style.css shared, ai/monitoring/setup page-specific)
└── js/
    ├── core/            nav.js, auth.js — every authenticated page
    ├── dashboard/       app, grid, events, faces, unknowns, zones, browse,
    │                    conditions, settings — index + single share these
    └── pages/           ai, cameras, monitoring, setup, telegram — 1:1 with HTML
```

| HTML | JS loaded | Backend APIs hit |
|---|---|---|
| `index.html` (home grid) | `js/dashboard/{grid,conditions,faces,events,settings,browse}.js` + `js/core/{nav,auth}.js` | /api/cameras, /api/events, /api/conditions, /api/faces |
| `single.html` (detail) | `js/dashboard/{app,events,faces,unknowns,zones,conditions,browse}.js` + `js/core/{nav,auth}.js` | WebSocket /ws/live?camera=, /api/config, /api/zones, /api/events, /api/faces, /api/unknowns |
| `cameras.html` | `js/pages/cameras.js` + `js/core/nav.js` | /api/cameras*, /api/cameras/discover, /api/cameras/onvif-stream-uri, /api/cameras/test-rtsp, /api/cameras/{id}/status |
| `ai.html` | `js/pages/ai.js` + `js/core/nav.js` | /api/ai/{config,status,history,chat,reset,vision*}, /api/recordings/* (DVR tab) |
| `monitoring.html` | `js/pages/monitoring.js` + `js/core/nav.js` | /api/monitoring/health + embedded Grafana iframe |
| `telegram.html` | `js/pages/telegram.js` + `js/core/nav.js` | /api/notifications/status (decides connect vs manage panel), /api/telegram/*, /api/setup/telegram/* (connect flow reuses wizard endpoints) |
| `setup.html` | `js/pages/setup.js` | /api/setup/{detect-hardware,apply-config,discover-cameras,telegram/*,complete}, /api/cameras*, /api/stats (verify step) |
| `login.html` | (inline only) | /api/auth/login, /api/login-bg |

**Cache-busting:** every `<script src="...js?v=N">` and `<link href="...css?v=N">` MUST be bumped when changing the asset. Browsers cache aggressively; missed bumps cause silent stale-code bugs. Server.py's `NoCacheHtmlStaticFiles` class sends `Cache-Control: no-cache` on `.html` responses but `.js`/`.css` rely on the `?v=` query string.

### 6.6 `websocket.py`
- Endpoint `/ws/live?camera=<id>` — accepted first (to send close frames), then `validate_session` on `vl_session` cookie. Closes with code 4401 if invalid.
- Per-CONNECTION local state: `sticky_identities` dict (person_id → name), `zone_cache`. **These used to be function attributes — shared across connections — which caused tab-to-tab label corruption. Now strictly local.**
- Renders bboxes + names + zones + action labels onto JPEG using `detection_frame:{type}:{cam}` (the frame the inference ran on, not the latest) to prevent drift.

---

## 7. Setup wizard (first-run)

### Gate
- `/data/setup-state/setup.json` absence → middleware redirects to `/setup.html`.
- `auto_mark_complete_if_preexisting()` on startup writes setup.json if registry has ≥1 camera, so upgrades skip the wizard.
- To re-run intentionally: `docker exec vision-labs-dashboard-1 rm /data/setup-state/setup.json && docker compose restart dashboard`.

### Flow (7 steps as of 2026-05-19)
1. **Welcome.**
2. **Hardware detect** — `POST /api/setup/detect-hardware` publishes `setup:probe-request` to orchestrator. Orchestrator runs one-shot `nvidia-smi` container, returns GPU list via `setup:probe-result` stream. Wizard recommends tier (small/mid/full) based on biggest VRAM; offers single-GPU or dual-GPU split chooser when ≥2 cards. Shows a "I'd rather have more cameras than AI chat" checkbox when disabling chat would meaningfully bump slot count. Click **Apply this configuration** → `POST /api/setup/apply-config` writes .env keys + publishes `config:apply` (orchestrator recreates affected services).
3. **Location + retention** — IANA timezone (full list from `available_timezones()`, grouped by region), DVR/snapshot/clip retention days. Same `/api/setup/apply-config` path.
4. **First camera** — Scan subnet button (ONVIF unicast scan) + manual RTSP fallback. Camera ID is locked to the next available slot (cam1 first). Test connection runs `ffprobe`. Skip jumps straight to Telegram.
5. **Verify** — Polls `/api/stats?camera=<id>` every 2s for 30s. ✓ when `frames_in_stream > 0` (pipeline is end-to-end alive), ✗ on timeout (pointer to Cameras tab to fix RTSP). Skippable.
6. **Telegram (optional)** — 3 substeps: paste bot token → backend validates via `getMe` → user sends `/start` to the bot → backend polls `getUpdates` for ~30 s to capture `chat_id` + `user_id` → writes both to `.env` via env_writer (TELEGRAM_BOT_TOKEN/CHAT_ID/ALLOWED_USERS are in the ALLOWED_KEYS allowlist). Sends a confirmation message. Skippable. The Telegram page (`/telegram.html`) shows this same flow inline when no token is configured.
7. **Finish** — `POST /api/setup/complete` writes setup.json. Dashboard becomes accessible.

The step indicator at the top is clickable for any already-visited step; forward jumps are blocked so required state (hardware probe before verify, camera before verify) can't be skipped.

### State file
```json
{
  "version": 1,
  "completed_at": "2026-05-18T03:30:00Z",
  "steps": ["hardware_detected", "camera_added", ...],
  "hardware": {"gpus": [{"index": 0, "name": "RTX 3060", "vram_mb": 12288}]}
}
```

---

## 8. Telegram pipeline

### Polling
`poll_telegram_callbacks()` long-polls `getUpdates`. Persists offset to Redis `telegram:last_offset` after EVERY processed update (so crash mid-batch is recoverable). Registers `setMyCommands` on startup for tab-complete.

### Authorization
Users in `telegram:users` hash (managed via dashboard `/api/telegram/users`). Unauthorized commands silently dropped + logged + emit `unauthorized_access` event to the primary camera's events stream.

### Commands (all in `routes/bot_commands/`, one file per command)
- Admin only (role=admin): `/arm`, `/disarm` (toggle notify_person + notify_vehicle on primary config).
- User: `/snapshot [cam]`, `/clip [Ns] [cam]`, `/status [cam]`, `/who [cam]`, `/events [N] [cam]`, `/zones [cam]`, `/timelapse [YYYY-MM-DD] [cam]`, `/analyze [cam] [prompt]`, `/ask <q>`, `/rules`, `/night`, `/faces`, `/cameras`, `/start`, `/help`.
- **Camera token parsing** — accepts cam id (`cam2`), friendly name (`basement`), prefix (`base`), or `all`. Bare commands with multiple cameras send an inline keyboard "tap to pick" with callback_data `cmd:<name>:<cam>` that re-dispatches a synthetic command. Commands that opted into this picker: `/snapshot`, `/clip`, `/zones` (added 2026-05-20).
- Photo handler runs MiniCPM-V on user-uploaded images.

### Broadcast (`routes/notifications/`)
- `_get_all_chat_ids()` walks `telegram:users` hash; falls back to `TELEGRAM_CHAT_ID` env.
- `broadcast_photo` iterates all chat_ids, increments `vl_notifications_total{camera, type}`.
- Cooldowns persisted to Redis hash `notify:last`. `_get_cooldown` floors at 10s regardless of user config.
- **HD bbox scaling** — pose detector runs on the small sub-stream. When attaching HD frames (`frame_hd:{cam}`), `draw_bbox_on_frame` checks `width >= 1000px` and scales bbox from SD→HD.
- `describe_scene(frame, prompt)` — MiniCPM-V via Ollama, strips `<think>` tags, `keep_alive=OLLAMA_KEEP_ALIVE` (5m default).
- `build_clip` records frames from `frames:{cam}` then re-encodes to H.264 via ffmpeg `libx264 +faststart` (OpenCV's mp4v doesn't play inline in Telegram).

---

## 9. AI assistant (Qwen 3 14B chat + MiniCPM-V vision)

### Storage
- `/data/ai.db` (SQLite, volume `auth-data`). Tables: `ai_config` (single-row), `reminders`, `chat_history`.
- Chat history window sent to Ollama is **6 messages** (3 user + 3 assistant) — was 20, cut on 2026-05-19 because long history was the #1 driver of Qwen regurgitating stale wrong answers instead of calling tools fresh. The client-side history view shows the full conversation; only the bus to Ollama is truncated.

### Reliability ceiling
Qwen 3 14B is the model — no swap to larger Ollama models or hosted Claude. The model is reliable for single-purpose questions ("how many vehicles yesterday?", "who was seen at 5pm?"). Compound multi-part questions still drift even with prompt + tool-result tuning. The AI tab suggestion chips were rewritten to single-purpose questions on 2026-05-19; visible tip in the UI: "💡 ask one thing at a time. Compound questions get muddled." Server-side enforcement loop in `routes/ai.py` detects DVR-link text without a real URL and re-prompts the model to append the link — caught a frequent hallucination class.

### Ollama config
- `OLLAMA_HOST=http://ollama:11434`. `OLLAMA_CHAT_MODEL=qwen3:14b` (env, empty disables chat). `OLLAMA_VISION_MODEL=minicpm-v` (env, empty disables vision). `OLLAMA_KEEP_ALIVE=5m`. `OLLAMA_NUM_CTX=8192`.
- GPU placement via compose: `NVIDIA_VISIBLE_DEVICES=${CHAT_GPU}` + `device_ids: ['${CHAT_GPU:-0}']`. Default `0` (single-GPU); set `CHAT_GPU=1` on dual-GPU.

### The 19 tools (`routes/ai_tools/`, one file per tool)
1. `query_events` — recent events (newest first, max 50). Returns events + `by_type` + `by_identity` + `unique_people_identified` aggregations + `truncated` flag. Defaults to `camera="all"`.
2. `query_faces` — list enrolled people (deduped by name, grouped photos).
3. `send_telegram` — message with optional snapshot/clip; accepts `camera` arg, echoes `source_camera_id` back.
4. `schedule_reminder` — DB write to `ai.db` reminders.
5. `get_system_status` — stream lengths, config, notification prefs. Defaults to `camera="all"`.
6. `get_live_scene` — current `state:{cam}` for every registered camera + aggregated `identified_people_now` list + count.
7. `query_unknowns` — unknown faces with `truncated` flag + `unknowns_shown` / `unknown_count` distinction.
8. `query_events_by_date` — date filter. Returns `total_events`, `by_type`, `by_identity`, `unique_people_identified`, `per_camera.<cam>.by_identity`, latest 10 raw events. Accepts `category` filter (people / vehicles / faces / actions / security / all) and `event_type` for single-type filter. Defaults to `camera="all"`.
9. `query_zones` — per-camera or "all".
10. `browse_vehicles` — vehicle snapshots inlined into reply.
11. `get_weather` — OpenWeatherMap proxy.
12. `query_event_patterns` — hourly/daily/type_breakdown. Hourly returns: `busiest_hour`, `top_hours` (top 5 with full breakdown), `active_window`, `hourly_breakdown` (all 24 hours), `by_type_per_hour`, `by_identity_per_hour`, `per_camera_hourly`. `type_breakdown` pre-seeds zero counts for all 10 known event types. Accepts `date=`, `days_back=`, `category=` args. Defaults to `camera="all"`.
13. `capture_snapshot` — live camera frame WITH automatic MiniCPM-V vision pass; returns `vision_analysis` field + `context.source_camera_id` / `source_camera_name`. Pass `describe=false` to skip vision for ~3s faster response.
14. `capture_clip` — 5-second mp4 to `/data/snapshots/clips/{uuid}.mp4`.
15. `query_notification_history` — recent Telegram-triggered events with `by_type` + `by_identity` aggregations and `truncated` / `alerts_found_in_sweep`. Multi-camera aware via `camera=all`.
16. `query_activity_heatmap` — hour-of-day × day-of-week. Now always emits all 24 hours per day so quiet hours are explicit zeros instead of missing keys.
17. `show_faces` — embed enrolled face photos inline (per-person grouped, up to 3 each).
18. `analyze_image` — MiniCPM-V vision LLM (standalone; for ad-hoc analysis of the latest frame).
19. `find_dvr_segment` — resolves `camera+date+time` to a `.ts` segment and returns a deep-link URL (`/ai.html?tab=recordings&camera=&date=&segment=`) for the user to click. Does NOT extract or send video bytes. Typical workflow: AI calls `query_event_patterns` to find busy hour, then `find_dvr_segment` to hand the user a link.

**Known event types** (all enumerated in tool descriptions via `KNOWN_EVENT_TYPES` constant in `routes/ai_tools/_shared.py`): `person_appeared`, `person_left`, `person_identified`, `vehicle_detected`, `vehicle_left`, `vehicle_idle`, `face_enrolled`, `face_reconciled`, `action_changed`, `unauthorized_access`.

**Category groups** (semantic buckets via `EVENT_CATEGORIES` constant): `people` (person_appeared/left/identified), `vehicles` (vehicle_*), `faces` (face_enrolled/reconciled + person_identified), `actions` (action_changed), `security` (unauthorized_access), `all`.

### System prompt + chat flow
- System prompt has an explicit `⚠️ ABSOLUTE RULE` banner at the top instructing the model to always call tools for factual queries and never copy numbers/identities from earlier assistant messages.
- See "Reliability ceiling" above for the chat-history window size and compound-question caveats.
- `build_system_context()` (`ai_prompts.py`) aggregates zones + event-stream length across all registered cameras (not just primary). Face list is grouped by name (one entry per person, not one per photo).

### Media side-channel
Base64 images would blow the LLM context (51KB JPEG ≈ 53k tokens vs 8k limit). `ai_state.py` stashes media per-request_id under a lock; the chat handler injects `![image]` / `<video>` HTML into the FINAL reply (see the `collect_media` calls around the end of `/api/ai/chat` in `routes/ai.py`). The LLM never sees the bytes.

---

## 10. Docker Compose layout

### Per-cam profile block structure
Each cam slot has 7 services with `profiles: ["camN"]`:
1. `recorder-camN` (host network)
2. `camera-ingester-camN` (host network)
3. `pose-detector-camN` (GPU)
4. `vehicle-detector-camN` (GPU)
5. `face-recognizer-camN` (GPU, exposes 8081 internal-only)
6. `tracker-camN` (no GPU)
7. `vehicle-attributes-camN` (no GPU, exits cleanly if `detect_vehicle_attributes!=true`)

Slot block ranges in docker-compose.yml: cam1's `recorder-cam1` block starts around line 371, cam5 around line 957, cam20 around line 3012; the file is ~3200 lines after 2026-05-19's expansion. Run `grep -n "^  recorder-camN:" docker-compose.yml` to find the exact line for any slot — these numbers drift quickly. **Future work:** dynamic slot generation via orchestrator-written `docker-compose.override.yml` would remove the 20-slot ceiling entirely.

Detectors use `restart: on-failure` (NOT `unless-stopped`) so that a clean exit from the `detect_<type>=false` registry gate stays exited rather than restart-looping. Minor inconsistency: cam1's detector blocks still say `restart: unless-stopped` — holdover from when cam1 was the legacy primary. `grep -n "^  pose-detector-cam1:" docker-compose.yml` finds the start of the cam1 detector blocks if you want to verify.

### Always-on services (no profile)
- `redis`, `ollama`, `dashboard`, `prometheus`, `grafana`, `redis-exporter`, `dcgm-exporter`, `orchestrator`, `portainer`.

### Named volumes (line 1041 in docker-compose.yml)
| Volume | Mount | What's stored |
|---|---|---|
| `redis-data` | redis:/data | AOF + 2GB maxmemory allkeys-lru |
| `face-data` | face-recognizer-*:/data | SQLite faces.db (embeddings + photos) |
| `yolo-models` | pose/vehicle:/models | YOLO weights auto-downloaded |
| `insightface-models` | face-recognizer:/root/.insightface | buffalo_l model |
| `auth-data` | dashboard:/data | auth.db, ai.db, setup-state/setup.json |
| `ollama-models` | ollama:/root/.ollama | ~9.3 GB Qwen + MiniCPM-V |
| `prometheus-data` | prometheus | metrics history |
| `grafana-data` | grafana | dashboard state |
| `portainer-data` | portainer | Portainer admin user, settings |
| `qnap-snapshots` | dashboard:/data/snapshots | person + vehicle JPEGs |
| `qnap-events` | dashboard:/data/events | daily JSONL journal |
| `qnap-telegram` | dashboard:/data/telegram | Telegram media archive |
| `qnap-videos` | dashboard:/data/videos | reserved |
| `qnap-clips` | dashboard:/data/clips | AI/Telegram clips (3-day) |

The `qnap-*` volumes are plain local Docker volumes by default. `docker-compose.qnap.yml` overlay flips them to CIFS mounts pointing at `//QNAP_IP/vision-labs/<subdir>`.

### Bind mounts (host paths)
- **`./data/recordings`** → recorder:/recordings (RW) + dashboard:/data/recordings (RO). Survives EVERYTHING short of `rm -rf ./data`.
- **`./contracts`** → /app/contracts (RO) in every service.
- **`./services/dashboard/{static,routes,pollers,helpers,*.py}`** → bind-mounted RO into dashboard for fast iteration (no rebuild on Python edits — just `docker compose restart dashboard`).
- **`./.env`** → dashboard:/app/.env (RW) so env_writer can mutate it.

### Network mode
- `host` on: camera-ingester-*, recorder-*, prometheus, grafana, redis-exporter, dcgm-exporter.
- Default bridge on: everything else. They reach each other via Docker DNS names (`redis`, `ollama`, `face-recognizer-cam1`, etc.).

### Orchestrator extra privileges
- Mounts `/var/run/docker.sock` (only orchestrator + portainer).
- Mounts `./:/workspace:ro` AND `${PWD}:${PWD}:ro` — DinD quirk: the inner compose CLI verifies build-context paths exist at the absolute string the host daemon uses.
- `HOST_PROJECT_DIR=${PWD}` so `--project-directory` works for builds.

---

## 11. Storage / persistence model

**Critical for backup/restore decisions.**

| Data | Where | Survives `docker compose down`? | Survives `docker compose down -v`? | Survives full project rm? |
|---|---|---|---|---|
| Enrolled faces (embeddings + photos) | volume `face-data` → SQLite `/data/faces.db` | ✅ | ❌ | ❌ |
| Admin password + sessions + AI chat history + setup.json | volume `auth-data` | ✅ | ❌ | ❌ |
| Camera registry, configs, zones, event streams (last 5000), Telegram users | volume `redis-data` (AOF) | ✅ | ❌ | ❌ |
| Person/vehicle snapshots, daily event JSONL | volumes `qnap-snapshots`, `qnap-events` | ✅ | ❌ | ❌ |
| Telegram media archive | volume `qnap-telegram` | ✅ | ❌ | ❌ |
| Continuous DVR recordings | bind mount `./data/recordings/` | ✅ | ✅ | ❌ |
| YOLO / InsightFace / Ollama model caches | volumes `yolo-models`, `insightface-models`, `ollama-models` | ✅ | ❌ | ❌ (re-downloadable) |
| Prometheus metrics history, Grafana state | volumes `prometheus-data`, `grafana-data` | ✅ | ❌ | ❌ (low value) |

**For a "clean reinstall but keep faces" workflow:**
```bash
# BEFORE reinstall: snapshot everything important to one tarball
bash scripts/backup.sh                 # writes ./vl-backup-YYYYMMDD-HHMMSS.tar.gz
mv vl-backup-*.tar.gz ~/                # move OUT of the project dir before wiping

# Then wipe (this kills volumes too):
docker compose --profile cam1 --profile cam2 down -v
docker rmi $(docker images "vision-labs*" -q) 2>/dev/null
docker image prune -a -f

# Reinstall via install scripts (re-pulls images / rebuilds):
bash scripts/install-linux.sh

# AFTER reinstall (but before adding cameras): restore the tarball
bash scripts/restore.sh ~/vl-backup-YYYYMMDD-HHMMSS.tar.gz
```

`scripts/backup.sh` already includes `face-data` + `auth-data` + `redis-data` + `qnap-snapshots` + `qnap-events` + `qnap-telegram`. `scripts/restore.sh` is destructive (wipes + extracts).

---

## 12. Authentication model

- **No server-side session store.** Tokens are signed HMAC, fully self-contained.
- **Token format:** `username:must_change_flag:timestamp:hmac_sha256_signature` (auth.py:143). 4-part; old 3-part tokens are rejected (users re-login). `must_change_flag` is `1` when login detected the default admin/admin combo so the middleware can force a password rotation before letting the user past. Key from env `SECRET_KEY` or generated into `app_config` SQLite table.
- **Cookie:** `vl_session` (httponly, samesite=lax, max_age=86400, path=/).
- **`validate_session(token) → username | None`** — used by HTTP middleware AND WebSocket (websocket.py:112 — middleware doesn't intercept WS upgrades, so each handler must call it).
- **First-run admin/admin:** `init_auth_db()` creates the default user if the users table is empty. Login endpoint detects the combo and returns `must_change_password: true` — UI forces rotation before letting through.
- **`/api/login-bg`** (auth-exempt) returns a heavily-blurred (1/4 scale GaussianBlur 51,51 σ=30 quality 30) JPEG so the login page background can't be used for surveillance.

---

## 13. Hardware tiers (`tiers/*.env`)

Concatenate one onto `.env`:

| Tier | Target VRAM | POSE_MODEL | VEHICLE_MODEL | CHAT_MODEL | VISION_MODEL | TARGET_FPS |
|---|---|---|---|---|---|---|
| `small.env` | 6 GB (1660 Ti, 3050, 2060) | yolov8n-pose.pt | yolov8n.pt | *(empty — off)* | *(empty — off)* | 5 |
| `mid.env` (default) | 8–12 GB (3060, 4060, 4070) | yolov8s-pose.pt | yolov8s.pt | qwen3:7b | *(commented)* | 10 |
| `full.env` | 16+ GB single OR dual-GPU | yolov8s-pose.pt | yolov8s.pt | qwen3:14b | minicpm-v | 15 |

All three default `DETECTOR_GPU=0` and `CHAT_GPU=0`. Set `CHAT_GPU=1` for dual-GPU split. Setup wizard auto-picks values.

---

## 14. Key gotchas / non-obvious behavior

1. **`CUDA_DEVICE_ORDER=PCI_BUS_ID`** is set on every GPU service. Without it, `DETECTOR_GPU=0` could point at a different physical card after a driver update (PyTorch defaults to enumeration order, which is unstable). PCI_BUS_ID is stable across reboots.

2. **`os.replace` EBUSY workaround in `env_writer.py`** — Docker bind-mounted files (`.env:/app/.env:rw`) reject `rename` with errno 16 (EBUSY) because the destination IS a mount point. The writer catches this and falls back to direct truncate-write — not atomic but the file is tiny.

3. **FastAPI route ordering** — `GET /next-slot` MUST be declared before `GET /{camera_id}` in `cameras.py`. FastAPI walks declarations in order, so a path param would shadow the literal route. Same pattern applies anywhere you mix path-params with sibling literals.

4. **`detection_frame:*` vs snapshot keys are different things:**
   - `detection_frame:{type}:{cam}` is overwritten every inference — used by the WebSocket overlay to draw bboxes on the **exact frame the model saw** (prevents drift when newer frames have arrived during inference).
   - `*_snapshot:{cam}:{ts}` is written at EVENT EMISSION time with TTLs (person 2h, vehicle 24h) and used for Telegram + dashboard event-feed thumbnails.

5. **Per-camera config hot-reload** — every detector + tracker + ingester polls `config:{camN}` every 10–25 messages. No restart needed when changing slider settings. Hot paths: pose detector (confidence/min_keypoints/kp_conf), vehicle detector (vehicle_confidence_thresh), tracker (iou_threshold, lost_timeout, vehicle_idle_timeout, suppress_known), ingester (target_fps), WebSocket (target_fps for render rate), notifications (`notify_cooldown` for person events only — `vehicle_cooldown` was removed when `vehicle_idle` moved to position-based dedup; see §16).

6. **Identity stickiness has 2 layers:**
   - **Tracker** — once `TrackedPerson.identity_name` is set, only overwritten by non-empty new value.
   - **WebSocket** — per-CONNECTION `sticky_identities` dict (was a bug: used to be function attributes shared across connections; tab-to-tab corruption). Pruned when `person_id` drops from active set.

7. **Why dashboard has no Docker socket** — explicit security decision. Dashboard attack surface is huge (FastAPI + WS + REST + user-facing login). Orchestrator is tiny, no incoming HTTP. Docker control lives there; dashboard publishes pub/sub events.

8. **`xreadgroup` block parameter trap on vehicle stream** — `services/tracker/core/main.py` deliberately omits `block` (or sets it to None) on the vehicle stream read. `block=0` means "block forever"; without this, the tracker would deadlock on cameras with `detect_vehicles=false` because the vehicle stream stays permanently empty. Pose-stream read uses `block=500` so the loop checks both streams every 500 ms.

9. **Action events use debounce + sticky multiplier** — Action changes after `ACTION_DEBOUNCE_FRAMES=10` consecutive frames with the new label; once set, `ACTION_STICKY_MULTIPLIER=2` raises the threshold to 20 frames to change back. Eliminates posture-noise spam.

10. **Identity grace period** — when `suppress_known=1`, tracker defers `person_appeared` for `IDENTITY_GRACE_SECONDS=4.0`; if face-recognizer identifies the person during the window, the event is suppressed entirely.

11. **First-camera-goes-to-cam1** — `AVAILABLE_SLOTS = [f"cam{n}" for n in range(1, 21)]` in order; `next_available_slot()` walks it. The wizard's "first camera" always lands as cam1 after a fresh install.

12. **Face DB atomic cache swap** — `FaceDB._load_cache()` builds NEW lists, then assigns to `self._cache` / `self._unknown_cache` in single STORE_ATTR ops (atomic in CPython). Readers iterating OLD lists complete safely. This is what lets multiple face-recognizer containers sharing the same SQLite stay in sync — 30s refresh thread can run without lock contention.

13. **Telegram bot token leak prevention** — `services/dashboard/server.py` silences the `httpx` logger to WARNING near the top of the module. httpx logs every outbound URL at INFO, which would include `https://api.telegram.org/bot<TOKEN>/sendMessage`.

14. **WebSocket auth bypass guard** — middleware doesn't intercept WS upgrades, so `websocket.py` must `accept()` first (required to send a close frame), THEN validate cookie, THEN close with 4401 if invalid.

15. **Notification cooldown floor** — `_get_cooldown` floors any user-supplied value at 10s regardless of `notify_cooldown` config. Spam prevention.

16. **Vehicle "stationary" check + `vehicle_idle` notify dedup** — `TrackedVehicle.is_stationary` requires ≥5 center-history samples, then compares the CURRENT center against the MEDIAN of the rolling 20-sample history; drift must be < `max(20 px, bbox_w * 0.15)`. (The bbox-scaled threshold replaced a fixed 30 px; the median replaced first-sample comparison so YOLO's per-frame bbox jitter on a parked car doesn't accumulate as drift. Floor was bumped 8 → 20 px and the slope 10% → 15% after cam1 live data showed jitter-driven false motion calls re-emitting `vehicle_idle`.) One parked vehicle's TrackedVehicle = exactly one `vehicle_idle` event (gated by `idle_alerted` flag); the flag clears the next time `is_stationary` is False, so a car that drives off + comes back gets a fresh idle alert.

    At the **notification layer** (`routes/notifications/alerts.py:notify_vehicle_idle`), the dedup is **position-based**, not per-tracker: SETNX `notify:vehicle_idle:seen:{cam}:{grid_x}_{grid_y}` with 30-min TTL, grid = 100 px buckets on bbox center. This catches the case the tracker-side gate can't — the same physical car getting multiple tracker instances over time (tracker restart, ghost expiry, IoU identity swap with a passing car) all collapse to the same parking-spot dedup key, so it's one Telegram per spot regardless of how many tracker IDs cycle through. The dashboard's event poller (`pollers/events.py`) DELs that key on `vehicle_left`, so when a spot vacates the next vehicle parks there gets a fresh notification. **`VEHICLE_GHOST_TTL`** is 30 s (was 5), so effective occlusion grace is `LOST_TIMEOUT (10s) + GHOST_TTL (30s) = 40 s` — wider than realistic drive-by occlusions, narrower than the position-dedup TTL so the layers compose cleanly. Helper `_vehicle_position_dedup_key()` in `routes/notifications/_shared.py`.

17. **HD bbox scaling** — pose detector runs on the small sub-stream. When attaching HD snapshots (`frame_hd:{cam}`), `draw_bbox_on_frame` checks `width >= 1000px` and scales bboxes from SD→HD using actual SD dimensions.

18. **AI tool media side-channel** — see §9. LLM never sees image bytes; they're stashed in `ai_state.py` under a per-request lock and injected into the FINAL reply HTML.

19. **Docker-in-Docker mount quirk** — orchestrator needs `${PWD}:${PWD}:ro` AS WELL AS `./:/workspace:ro` because the inner compose CLI verifies build-context paths at the absolute string the host daemon uses. Without `${PWD}` mount, builds fail with "unable to prepare context: path not found".

20. **Snapshot path resolution is migration-safe** — `routes/events.py:resolve_event_snapshot_path` walks (1) per-camera dir, (2) every camera subdir from registry, (3) legacy flat path. Old installs with flat `/data/snapshots/<event>.jpg` still serve correctly.

21. **Daily JSONL journal complements Redis stream** — `events:{cam}` stream is capped at maxlen=5000. Long-term history lives in `/data/events/YYYY-MM-DD.jsonl`. `routes/events.py:_read_journal` falls through when Redis history is exhausted.

22. **Metrics labels matter for multi-cam** — `vl_*` counters were originally global; fixed to be labeled by `camera`. The collector's `_last_*_id_by_cam` dict tracks per-camera last-seen Redis stream IDs.

23. **DCGM exporter labels** — every GPU metric has `gpu` (index) and `modelName`. Grafana legendFormat uses both for clarity: `GPU0 util (NVIDIA GeForce RTX 5070 Ti)`. byName color overrides in the dashboard JSON are now no-ops (literal labels changed); Grafana falls back to default palette.

24. **ONVIF discovery is unicast, NOT multicast** — empirically multicast WS-Discovery silently fails in WSL2+Hyper-V even with mirrored networking + firewall rules. Unicast scan with a 50-thread pool sweeps a /24 in ~6 seconds. Documentation/install scripts that still mention "multicast" or "Hyper-V firewall rules for ONVIF auto-discovery" are vestigial — keep them only as best-effort.

25. **The detector "registry gate" exits cleanly** — pose/vehicle/face-recognizer all run `_check_camera_wants_detector()` at startup; clean exit when the flag is false. Compose uses `restart: on-failure` so the exit stays exited. Toggling `detect_*` in the UI **does** now auto-recreate the affected service: `cameras.py:upsert_camera` detects detector-flag changes vs the existing entry and publishes to `config:apply` with **pre-expanded** per-cam service names (`pose-detector-cam2`, `vehicle-detector-cam2`, `face-recognizer-cam2`). Note the orchestrator's original `_expand_per_cam_services` only handled BARE names (`pose-detector` → all-enabled-cams) — it had no concept of "target only this one camera." The pre-expanded path was added on top: `apply_config`'s allowlist accepts `{prefix}-{profile}` where prefix∈`PER_CAM_SERVICE_PREFIXES` and profile∈`ALLOWED_PROFILES` (helper: `_split_pre_expanded(svc)`); `_expand_per_cam_services` passes those through verbatim + records the camera's profile so compose can resolve the gated service. Pre-expanded names targeting a *disabled* camera drop silently — nothing to recreate. End-to-end: the edit pencil ✏ on each camera row (added 2026-05-21) opens a modal that lets users toggle detector flags + edit name/location; saving fires the PUT route → `upsert_camera` → `config:apply` → orchestrator → service comes up (or self-exits if newly false) within seconds. Before all this, `reconcile()` only watched `enabled` and toggling detector flags mid-life was a no-op.

26. **`build_clip` re-encodes to H.264** — OpenCV's mp4v (MPEG-4 Part 2) doesn't play inline in Telegram. `routes/notifications/frame.py` (the `build_clip` function) wraps the OpenCV output in ffmpeg `libx264 +faststart`.

27. **`network_mode: host` on Prometheus implies localhost-only scrape addresses** — `prometheus.yml` uses `localhost:8080`, `localhost:9121`, `localhost:9400`. Grafana also reachable at `localhost:3000` only.

28. **`EXTRA_COMPOSE_FILES` threads the registry overlay into orchestrator-issued compose calls.** Without it, a registry-pull install would silently rebuild cam2 services from source when the dashboard adds a new camera, because the orchestrator's compose CLI only sees `docker-compose.yml`. `install-linux.sh` writes `EXTRA_COMPOSE_FILES=/workspace/docker-compose.registry.yml` to `.env` on `--pull` (the default). The orchestrator reads it and prepends `-f <each-file>` to every compose invocation. Empty string on `--build` installs — no behavior change there.

29. **Hard line numbers throughout this doc are best-effort.** Treat file/symbol references as truth; if `services/dashboard/server.py:113` doesn't match what's at line 113 today, search the file for the referenced symbol instead. The compose file especially has shifted (cam1 used to be near the top; expanding to 20 slots pushed everything down).

30. **`vehicle_left` ≠ "the car drove out of frame."** As of the Phase 1 follow-up fix, the tracker emits TWO distinct track-end events:
    - `vehicle_gone` — **internal**, fires at every ghost-buffer expiry (drive-by AND idle-leave). Carries `was_idle: "True"|"False"`. Consumed by the vehicle-attributes service as its buffer-flush trigger. Never shown to users.
    - `vehicle_left` — **user-facing**, fires only when `veh.idle_alerted=True` was set during the track's life (the car was idle long enough to cross `VEHICLE_IDLE_TIMEOUT`, then drove off). Telegram + events panel render this.
    
    Before the fix, `vehicle_left` fired on every ghost expiry — including 0.5s drive-bys that never went idle. Combined with the IoU identity-swap bug (one physical car briefly splits into two `TrackedVehicle` IDs), the events panel got two "left" events for a single car driving past. The semantic split (gone vs left) cleaned up the user-facing noise without breaking the attribute pipeline.
    
    **Note:** the underlying IoU identity-swap bug is NOT fixed by this split — it just means drive-by-noise no longer compounds it. See `[[vehicle-left-double-fire-bug]]` memory note for the IoU follow-up work.

---

## 15. Recent architectural decisions

### Phase A (2026-05-17)
- **Removed ComfyUI service + Generate tab.** Image-generation use case dropped. Static `generate.{js,css,html}` deleted. ARCHITECTURE.md was de-staled in the 2026-05-19 doc audit (no remaining generate.js / generate.css references).

### Phase B (2026-05-17)
- **Single-GPU as default + hardware tiers.** `tiers/{small,mid,full}.env`. `DETECTOR_GPU` / `CHAT_GPU` env replace hardcoded device IDs. WSL2 GPU isolation fix: must set BOTH `NVIDIA_VISIBLE_DEVICES` AND `CUDA_VISIBLE_DEVICES`.

### Phase C (2026-05-17)
- **Shared base image + GHCR overlay.** `services/base/Dockerfile` shared by pose/vehicle/face. `docker-compose.registry.yml` pulls pre-built images for users who don't want to build.

### Phase C.2 (2026-05-17)
- **Dropped.** ONNX migration explored; benchmarked ONNX vs PyTorch on YOLO. No VRAM benefit (-15 MiB actually larger). Not worth the conversion complexity.

### Phase D (2026-05-17) + D.5 (2026-05-17)
- **First-run setup wizard** at `/setup.html`. Orchestrator-spawned `nvidia-smi` probe via pub/sub.
- **ONVIF unicast subnet scan** (`helpers/onvif_discovery.py`). Multicast confirmed broken on WSL2; unicast works everywhere.

### Phase E (2026-05-17)
- **Install scripts.** `scripts/install-linux.sh` + `scripts/install-windows.ps1`. PowerShell 5.1 compatibility (ASCII-only). WSL detection via `wsl --status` + `$LASTEXITCODE` (regex on UTF-16 output is unreliable). SIGPIPE fix on multi-GPU hosts (replaced `nvidia-smi | head -1` with `read -r < <(nvidia-smi ...) || true`).

### Phase F (2026-05-18)
- **Setup wizard writes .env + triggers service recreation.** `apply-config` endpoint + `env_writer.py` + `config:apply` pub/sub.

### Phase G (2026-05-18) — biggest refactor of the month
- **`front_door` → `cam1`. Symmetric slot model.** No more privileged primary camera. All slots are profile-gated and orchestrator-managed identically; originally 5 (cam1–cam5), expanded to 20 on 2026-05-19.
- Live migration ran on dev host: 104 Redis keys renamed, 718 events rewritten, 1012 identities rewritten, recordings dir + snapshot subdirs moved, event JSONLs rewritten in place.
- Stale references remaining: docs/history/REFACTOR_PLAN.md (historical doc), docs/history/PHASES.md.

### Phase H (2026-05-18)
- Camera-add form locks ID to next-slot (read-only input).
- Server-side validation rejects POST /api/cameras with id outside AVAILABLE_SLOTS (400).
- Universal modal-backdrop click closes all modals (nav.js handler with `_CLEANUP_BY_ID` map).
- Migration script `migrate-stream-fields.sh` to complete Phase G — rewrites front_door field VALUES inside stream entries (the main migration only renamed the keys).
- Prometheus stale-camera cleanup script (`prometheus-clean-stale-cameras.sh`).

### Phase H follow-ups (2026-05-18)
- Phase G removed RTSP env from dashboard service → `detect_local_cidr` returned None on fresh containers. Fixed: now reads `cameras:registry` first.
- Cache-bust miss on cameras.js?v=4 → v=5 (would have shown "all 5 slots in use" incorrectly).
- Load-older button always-show fix (was conditional on `hasNew=true`).
- Universal backdrop-close was running every cleanup function on every backdrop click — fixed with overlay-id→fn-name map.

### 2026-05-18 polish commits
- `/api/cameras/next-slot` route ordering bug (was returning 404 because `/{camera_id}` shadowed it).
- Load-older button visibility — explicit `display: "block"` / `"none"` instead of `""` (was inheriting flex container's display).
- Grafana panel legendFormat: GPU panels (util/temp/power) and VRAM panel now label as `GPU{{gpu}} ... ({{modelName}})`.

### Phase J — modularization (2026-05-19)
- **`ai_tools.py` → `routes/ai_tools/` package.** One file per `_tool_*` entrypoint, plus `_shared.py` for KNOWN_EVENT_TYPES / EVENT_CATEGORIES / `_resolve_camera` etc., plus `__init__.py` aggregating SCHEMAS into the `TOOLS` list for the chat endpoint.
- **`bot_commands.py` → `routes/bot_commands/` package.** `_poller.py` long-poll loop, `_dispatch.py` router, one file per command (`/snapshot`, `/clip`, `/status`, `/zones`, `/events`, `/analyze`, `/ask`, `/timelapse`, `/who`, `/faces`, `/cameras`, `/rules`, `/night`, `/arm`, `/disarm`, `/help`, `/start`), `_shared.py` exporting send-helpers + camera-token parsing.
- **`notifications.py` → `routes/notifications/` package.** Split into `_shared.py`, `telegram_api.py`, `frame.py`, `scene.py`, `alerts.py`, `endpoints.py`. External surface unchanged.
- **`services/tracker/tracker.py` → `services/tracker/core/`.** `core/main.py` (run loop), `core/state.py` (TrackedPerson + TrackedVehicle dataclasses), `core/manager.py` (PersonTracker orchestrator), `core/iou.py`, `core/config.py`. `tracker.py` at the COPY root is now a thin shim — Dockerfile + CMD unchanged.

### Phase J fallout (2026-05-19 / 2026-05-20)
The AST-driven splits walked top-level defs but did NOT follow the call graph or module-level name references. Two regression incidents:

1. **`_load_jsonl_journal` lost from `query_events_by_date`** (2026-05-19) — past-date queries crashed silently inside tool results. Soft-failure UX masked the bug for a week. Fix: restored the helper + added `tests/test_ai_tools_no_nameerror.py` (21 tests, one per tool).
2. **Six bot commands lost module-level imports** (2026-05-20) — `/events`, `/status`, `/analyze`, `/ask`, `/timelapse` lost `make_redis_client`, `REDIS_HOST/PORT`, `OLLAMA_*`, `SNAPSHOT_DIR`; `/clip` lost cross-module `_extract_clip_frames` + `_describe_scene_multi` from `analyze.py`. Surfaced as NameError when users invoked the commands. Fix: routed all shared constants through `bot_commands/_shared.py` and added each name to its consuming file's import list. Regression guard: `tests/test_bot_commands_no_nameerror.py` (added 2026-05-20) — captures `send_text` calls and asserts no regression-class error text leaks through the try/except wrappers that originally hid the bug.

See CLAUDE.md §0 for the canonical lesson.

### Phase K — GHCR live (2026-05-20)
- **`.github/workflows/publish-images.yml`** now active. `v*` tag push → builds shared base + 8 service images on `ubuntu-latest` runners → publishes to `ghcr.io/gammahazard/vision-labs/<service>` with `:vX.Y.Z` and `:latest` tags. First publish takes ~25-40 min; subsequent tag pushes ~9-10 min thanks to Docker layer cache.
- **`docker-compose.registry.yml`** overlay extended from cam1-cam5 to cam1-cam20 to match the base compose.
- **`scripts/install-linux.sh` flipped to pull-from-GHCR by default.** `--build` (or `BUILD_FROM_SOURCE=1`) keeps the local-build path for forkers. `IMAGE_TAG=v0.1.0 bash scripts/install-linux.sh` pins to a specific release.
- **`EXTRA_COMPOSE_FILES` env var on orchestrator.** Installer writes `EXTRA_COMPOSE_FILES=/workspace/docker-compose.registry.yml` to `.env` on a pull install so the orchestrator's later `docker compose --profile camN up -d` calls keep pulling instead of building. Without this, adding cam2 through the dashboard on a pull install would silently rebuild.
- Tags shipped: **v0.1.0** (first publish), **v0.1.1** (vision-model auto-pull + bot_commands NameError fixes + /zones camera picker + sharper setup GIF).
- **One-time per package after first publish**: flip each GHCR package from private to public via GitHub → Packages → ⚙️ → "Change package visibility". Required before strangers can `docker pull`.

### AI assistant reliability tuning (2026-05-19/20)
Qwen 3 14B is the model ceiling on a single-host install — no swap to larger models or hosted Claude allowed. Iterations to make compound questions less unreliable:
- Chat history window cut from 20 → 6 messages (the #1 cause of stale wrong answers was the model regurgitating from earlier turns instead of calling tools fresh).
- System prompt gained an explicit `⚠️ ABSOLUTE RULE` banner at the top + a few-shot example showing the expected answer shape for compound questions.
- `query_events_by_date` now returns a pre-composed `summary` field (LLM tends to grab `total_events` when it should grab `detection_count`) and trims `latest_events` from 10 → 3 to save attention budget.
- Server-side enforcement loop in `routes/ai.py` detects DVR-link text without a real URL and re-prompts with "append the link, keep the rest verbatim".
- AI tab suggestion chips rewritten to single-purpose questions. Visible tip: "💡 ask one thing at a time. Compound questions get muddled." This is the honest framing — the 14B model is fine for one focused question per turn but compound multi-part questions still drift.

---

## 16. File index (where to look)

### Top of repo
- `docker-compose.yml` — main compose file. 10 always-on services + 7 per-cam services × 20 profile-gated slots = 140 profile-gated. ~104 KB.
- `docker-compose.registry.yml` — overlay to pull pre-built images from GHCR
- `docker-compose.qnap.yml` — overlay to flip qnap-* volumes to CIFS mounts
- `.env` / `.env.example` — runtime config
- `README.md` — user-facing docs (refreshed 2026-05-20 with screenshots + Mermaid diagram + setup/Grafana GIFs)
- `CONTEXT.md` — this file (start here for full context)
- `CHANGELOG.md` — Keep-a-Changelog format, one entry per shipped behavior change. Mirrors tag annotations. New as of 2026-05-20.
- `CLAUDE.md` — AI-assistant operational conventions for this repo (R3-split lessons, build/runtime split, release flow). Auto-loaded into Claude Code sessions.
- `DETAILED_README.md` — in-depth setup + operations guide
- `ARCHITECTURE.md` — architectural reasoning (why services are split this way)
- `docs/history/` — historical planning docs: PHASES.md, REFACTOR_PLAN.md, PACKAGING_PLAN.md, MANUAL_SETUP.md

### Source
- `contracts/{streams,actions,time_rules}.py` — shared schemas/algorithms
- `services/base/` — shared CUDA Dockerfile
- `services/camera-ingester/ingester.py` — RTSP → frames:{cam}
- `services/pose-detector/detector.py` — YOLOv8s-pose
- `services/vehicle-detector/detector.py` — YOLOv8s (filtered classes)
- `services/face-recognizer/{recognizer.py, face_db.py}` — InsightFace + SQLite
- `services/tracker/` — IoU tracker. `tracker.py` is a thin entrypoint shim; real code lives in `core/` (split 2026-05-19): `core/state.py` (TrackedVehicle, TrackedPerson), `core/manager.py` (PersonTracker orchestrator), `core/iou.py`, `core/main.py` (run loop), `core/config.py`.
- `services/recorder/recorder.py` — ffmpeg .ts segments
- `services/orchestrator/orchestrator.py` — Docker control
- `services/dashboard/`
  - `server.py` — FastAPI app + startup
  - `websocket.py` — `/ws/live` handler
  - `cameras.py` — `AVAILABLE_SLOTS` + helpers
  - `routes/` — 18 router files + 3 split packages (ai_tools/, bot_commands/, notifications/); see §6.2
  - `helpers/{env_writer,onvif_discovery,geometry}.py`
  - `pollers/{events,health,ollama_warmup,reminders,retention}.py`
  - `static/` — 8 HTML pages + JS/CSS in subdirs (see §6.5)
  - `constants.py`, `event_renderer.py`, `ai_db.py`
- `services/prometheus/prometheus.yml`
- `services/grafana/{provisioning,dashboards}/`

### Tiers
- `tiers/{small,mid,full}.env`

### Scripts
- `scripts/build.sh` — build base image + everything
- `scripts/backup.sh` — tar 6 volumes to ./vl-backup-<timestamp>.tar.gz
- `scripts/restore.sh` — destructive restore
- `scripts/install-linux.sh` — full Ubuntu/Debian installer
- `scripts/install-windows.ps1` — Windows installer (sets up WSL2)
- `scripts/migrate-front-door-to-cam1.sh` — Phase G one-shot migration (already run on dev host)
- `scripts/migrate-stream-fields.sh` — Phase H follow-up (rewrites stream entry field values)
- `scripts/prometheus-clean-stale-cameras.sh` — tombstone stale-camera Prom labels

### Tests
- 377 tests, 0 quarantined as of 2026-05-21. Run via `source .venv-test/bin/activate && pytest -q`.
- `tests/` files: `test_actions.py`, `test_ai_tool_aggregations.py`, `test_ai_tools_no_nameerror.py` (R3-split regression guard for ai_tools), `test_bot_commands_no_nameerror.py` (R3-split regression guard for Telegram bot commands — includes a recorder that captures `send_text` calls and asserts no `"is not defined"`/`"has no attribute"`/`"cannot import name"` text leaks through a try/except-wrapped NameError), `test_face_db.py`, `test_notifications.py`, `test_routes.py`, `test_scene_analysis.py`, `test_time_rules.py`, `test_tracker.py`, `test_vehicles.py`.
- `FakeRedis` (in `test_vehicles.py`) is the standard stub. Tests use host Python 3.12; container is 3.11.

---

## 17. Common operations

### Add a camera (UI flow)
1. Cameras tab → form auto-fills next slot (cam1 first).
2. Manual RTSP URL or Scan Network (ONVIF unicast).
3. Test Connection (runs ffprobe).
4. Save → upserts `cameras:registry`, publishes `cameras:events`.
5. Orchestrator reconciles within 10s → `compose --profile camN up -d <services>`.
6. Status badge polls `/api/cameras/{id}/status` (reads `orchestrator:audit` latest entry).

### Add a camera (CLI, debugging)
```bash
docker exec vision-labs-redis-1 redis-cli HSET cameras:registry cam2 '{"id":"cam2","name":"basement","rtsp_sub":"rtsp://...","rtsp_main":"rtsp://...","enabled":true,"detect_persons":true,"detect_vehicles":true,"detect_faces":true,"gpu_id":0}'
docker exec vision-labs-redis-1 redis-cli PUBLISH cameras:events 'cam2 added'
# Orchestrator picks it up within 10s.
```

### Enroll a face
1. UI: home → Known Faces panel → Enroll wizard, OR Cameras detail → Unknowns gallery → Label.
2. POST `/api/faces/enroll` (multipart photo + name) → face-recognizer crops, embeds, INSERT into faces.db known_faces, retroactively reconciles unknown_faces table.
3. All face-recognizer containers see the new face within `CACHE_REFRESH_INTERVAL=30s`.

### Re-run the setup wizard
```bash
docker exec vision-labs-dashboard-1 rm /data/setup-state/setup.json
docker compose restart dashboard
```

### Backup before risky work
```bash
bash scripts/backup.sh
# → vl-backup-<timestamp>.tar.gz in $(pwd). Move it out of the project dir.
```

### Restore (destructive — overwrites all volumes)
```bash
bash scripts/restore.sh ~/vl-backup-20260518-153000.tar.gz
```

### Force-rebuild after Python changes
Dashboard's Python is bind-mounted RO — just `docker compose restart dashboard` (5s). Other services need rebuild:
```bash
docker compose --profile cam1 build pose-detector-cam1
docker compose --profile cam1 up -d pose-detector-cam1
```

### Fresh install (default: pull from GHCR)
```bash
git clone https://github.com/gammahazard/vision-labs-v2 vision-labs && cd vision-labs
bash scripts/install-linux.sh                # default — pull from GHCR (~3-5 min)
bash scripts/install-linux.sh --build        # force local build (forkers, ~10-15 min)
IMAGE_TAG=v0.1.1 bash scripts/install-linux.sh  # pin a specific release
```

### Cut a new release (maintainer)
```bash
# 1. Update CHANGELOG.md — move [Unreleased] into [vX.Y.Z] section, add a fresh [Unreleased]
# 2. Commit with subject "release: vX.Y.Z"
# 3. Tag with an annotation mirroring the CHANGELOG section
git tag -a v0.2.0 -m "v0.2.0 — release notes here"
git push origin v0.2.0
# 4. Wait ~9-10 min for .github/workflows/publish-images.yml to finish
# 5. Verify packages at https://github.com/gammahazard?tab=packages
# 6. First-publish only: flip each new package to public via Package Settings
```

### Tail orchestrator decisions
```bash
docker exec vision-labs-redis-1 redis-cli XREVRANGE orchestrator:audit + - COUNT 20
```

### Clean stale-camera Prometheus series after removing a camera
```bash
YES=1 bash scripts/prometheus-clean-stale-cameras.sh
```

### Wipe everything + start fresh but keep faces
See §11 for the full sequence. TL;DR: `backup.sh` → `down -v` → reinstall → `restore.sh`.

---

## 18. What's NOT in this codebase (and likely won't be)

- macOS / Apple Silicon support — CUDA-bound pipeline.
- Multi-host clustering — single-host Docker Compose by design.
- Cloud egress — no S3/GCS/Azure upload paths.
- Per-camera GPU placement — `DETECTOR_GPU` is global (all camN detectors land on the same GPU). Manual edit of compose blocks possible but unsupported.
- Live model swap — changing `POSE_MODEL` requires service recreate (handled by `apply-config`).
- Real-time RTP recording without ffmpeg — recorder is intentionally a separate process.

---

## 19. Debugging hotspots

When a feature breaks, here's where it usually broke:

| Symptom | First thing to check |
|---|---|
| Camera not coming up after Add | `XREVRANGE orchestrator:audit + - COUNT 10` for the failed action. Common cause: ALLOWED_PROFILES doesn't include the slot. |
| Live view blank | WebSocket cookie auth — check browser devtools network for `/ws/live` close code 4401. |
| Names not sticking to bboxes | Tracker `_update_identities` IoU threshold; WebSocket sticky_identities (per-conn). |
| Telegram silent | `/api/telegram/users` populated? `telegram:last_offset` advancing? httpx logs at INFO would leak the bot token — confirm `logging.getLogger("httpx").setLevel(logging.WARNING)` is still at the top of `services/dashboard/server.py`. |
| ONVIF scan returns 0 | `detect_local_cidr()` chain. Run `docker exec vision-labs-dashboard-1 python -c "from helpers.onvif_discovery import detect_local_cidr; print(detect_local_cidr())"`. |
| Setup wizard 422 | `Depends(validate_session)` regression — `validate_session` is a plain helper, not a FastAPI dep. Endpoints in `routes/setup.py` must NOT use `Depends(validate_session)`. |
| .env writes silently dropped | `env_writer.py` ALLOWED_KEYS allowlist. Adding a new key requires updating the set. |
| Recorder stuck in restart loop | RTSP URL — env vs registry. Check `docker logs vision-labs-recorder-camN-1`. |
| Stale faces showing on a camera | Per-container cache refresh — wait 30s OR restart that face-recognizer container. |
| Prometheus shows ghost cameras | Run `prometheus-clean-stale-cameras.sh` to tombstone. |

---

End of CONTEXT.md. When in doubt about what something does or how it talks to other services, search this file first — but trust the source code over the doc if they disagree.
