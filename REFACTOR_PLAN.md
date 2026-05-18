# Vision Labs — Refactor Plan

> **Goal:** Make the codebase modular enough to debug easily and add cameras (and eventually a HomeKit/TV dashboard) without rewriting everything. Zero behavior change — pure structural moves.
>
> **Rule:** Every step is a pure mechanical move that doesn't change runtime behavior. After every step we rebuild + restart + confirm dashboard still serves frames. If a step is hard to make behavior-preserving, we stop and ask before continuing.
>
> **Historical note (post Phase 8.A):** This doc references ComfyUI / `image_gen.py` / `gpu:generation_active` / `comfyui_cleanup.py` throughout because it documents the dashboard split as it existed when that service was alive. ComfyUI and the entire image-generation surface were removed in Phase 8.A of the packaging work (see PACKAGING_PLAN.md and PHASES.md Phase 8). The structural reasoning here is still valid; just mentally elide the ComfyUI line items.

---

## ⚡ RESUME HERE — current state (as of `ed2c3c2`, May 10 2026)

If you're picking this up cold, read this section first.

### Live system topology

**Hardware (dev environment, for reference only — yours will differ):**
- Dashboard host: WSL2 Ubuntu 24.04 on Windows, 5070 Ti (GPU 0) + 3090 (GPU 1)
- Camera 1: Reolink RLC-1240A on the main LAN (slot `front_door`)
- Camera 2: Raspberry Pi 5 + Logitech C922 USB webcam on a side subnet (slot `cam2`)

**Pi 5 setup (one possible cam2 source):**
- mediamtx publishes `rtsp://<pi-ip>:8554/<stream-name>` at 1280×720 @ 10 FPS
- Auto-starts on boot via `/etc/systemd/system/mediamtx.service`
- Config at `/home/<user>/mediamtx.yml`

**Camera registry** (Redis `cameras:registry` hash) — two entries:
```
front_door  → <camera-name>,  rtsp://admin:.../h264Preview_01_sub,  all 3 detectors on
cam2        → basement,        rtsp://<pi-ip>:8554/basement,         pose + face only (no vehicle)
```

**Disk layout (current — all per-camera):**
```
./data/recordings/{camera_id}/{date}/HH-MM.ts   ← bind mount, MPEG-TS, 1h segments, 3-day retention
                                                  Windows path: \\wsl$\<distro>\…\data\recordings\
/data/snapshots/{camera_id}/{event_id}.jpg      ← Docker volume qnap-snapshots, 4-day retention
/data/snapshots/vehicles/{camera_id}/{date}/…    ← same volume, per-camera + date subdirs
/data/snapshots/clips/                          ← AI + /clip outputs, 3-day retention
/data/events/{date}.jsonl                       ← Docker volume qnap-events, each entry tagged with camera
/data/telegram/{user_id}/                       ← Docker volume qnap-telegram, audit copies
```

### File layout (post-refactor, all verified to exist)

```
services/dashboard/
├── server.py              (353 lines — wiring only: imports, app, middleware, startup, static mount)
├── constants.py            (Ollama + ComfyUI defaults, env-overridable)
├── websocket.py            (522 lines — /ws/live; accepts ?camera=<id> query param)
├── cameras.py              (211 lines — CameraRegistry: list/get/upsert/delete + slot allocation)
├── ai_db.py                (chat history SQLite)
├── Dockerfile
├── helpers/
│   └── geometry.py         (bbox_iou + in_dead_zone — used by websocket.py)
├── pollers/
│   ├── reminders.py         (69 — Telegram reminders every 60s)
│   ├── ollama_warmup.py     (95 — pulls Qwen 3 14B, warms GPU)
│   ├── comfyui_cleanup.py   (73 — clears stale ComfyUI queue + gpu:* locks at startup)
│   ├── retention.py         (~170 — prunes per-camera snapshots, clips, vehicle dirs, events)
│   └── events.py            (397 — multi-camera fan-out poller, watches every events:{cam} stream)
├── routes/
│   ├── __init__.py          (shared context: r, r_bin, stream key constants)
│   ├── auth.py              (login, logout, change-password, forced rotation)
│   ├── ai.py                (chat completion loop)
│   ├── ai_tools.py          (1641 lines — 18 LLM tool defs + executors, all camera-aware)
│   ├── ai_prompts.py        (system prompt builder; injects camera list into context)
│   ├── ai_state.py          (shared AI assistant state)
│   ├── bot_commands.py      (1902 lines — Telegram polling + 15 commands w/ camera args)
│   ├── notifications.py     (961 lines — Telegram API helpers, build_clip(camera_id=))
│   ├── events.py            (225 lines — /api/events?camera= aggregate or filter)
│   ├── config.py            (~100 — /api/config?camera= per-camera config)
│   ├── zones.py             (~115 — /api/zones?camera= per-camera CRUD)
│   ├── cameras.py           (REST API: GET/POST/PUT/DELETE /api/cameras, test-rtsp endpoint)
│   ├── recordings.py        (per-camera DVR API + /api/recordings/cameras lister)
│   ├── browse.py, clips.py, conditions.py, faces.py, unknowns.py
│   ├── image_gen.py, metrics.py, telegram_access.py
└── static/
    ├── index.html          (multi-cam GRID home + Recent Activity feed + Conditions + Faces)
    ├── grid.js              (per-tile WebSocket + modal logic, click → /single.html?camera=X)
    ├── single.html         (per-camera detail view, wired to ?camera= URL param)
    ├── app.js               (CAMERA_ID from URL, withCamera() helper, live-title update)
    ├── events.js            (reads camera from URL, badge on aggregate feed)
    ├── zones.js             (per-camera zones via withCamera())
    ├── ai.html, ai.js       (AI chat + DVR tab with per-camera picker)
    ├── cameras.html, cameras.js (registry admin UI)
    ├── faces.js, conditions.js, browse.js, unknowns.js, auth.js
    ├── telegram.html, monitoring.html, login.html
    └── style.css
```

### Phase status — what's done, what's next

**✅ Done (35+ commits, dashboard never broke during the refactor):**
| Phase | Result |
|-------|--------|
| 1 — Surgical bug fixes | Real metrics.py `state.persons` bug fixed |
| 2 — Constants module | 13 hardcoded literals consolidated |
| 3 — Helper module | geometry.py extracted |
| 4 — Extract 5 pollers | reminders, ollama_warmup, comfyui_cleanup, retention, events |
| 5 — Extract WebSocket | websocket.py — sticky-identity bug fixed for free |
| 6 — server.py shape | 1313 → 353 lines (73% smaller) |
| 7 — Camera registry | cameras.py + /api/cameras |
| 7b — Camera management UI | cameras.html admin form + ffprobe test |
| 7c — Slot-based services | cam2 slot in compose; detectors respect detect_X flags |
| 8b iter 1 — Grid view | /index.html is now the grid; /single.html is the detail view |
| 8b iter 1.1 — Home panels | Conditions + Known Faces panels below the grid |
| 9a iter 1 — AI multi-camera (3 tools) | get_live_scene aggregates; query_events + capture_snapshot take `camera` arg; system prompt lists cameras |
| 9a iter 2 — AI multi-camera (all 18 tools) | All remaining tools (`query_events_by_date`, `query_zones`, `browse_vehicles`, `query_event_patterns`, `query_activity_heatmap`, `capture_clip`, `get_system_status`) accept `camera` arg via `_resolve_camera()` helper |
| 9b iter 1 — Telegram multi-camera | `/snapshot [camera]`, `/clip [N] [camera]`, `/events [N] [camera]`, `/who`, `/zones`, `/timelapse`, `/analyze`, `/status` all parse camera token (id/name/fuzzy/`all`); new `/cameras` helper command; inline-keyboard camera picker for bare `/snapshot` and `/clip` |
| 9b iter 2 — Event poller fan-out | `pollers/events.py` now watches every enabled camera's `events:{id}` stream via multi-stream xread; refreshes registry every ~60s so added cameras are picked up live; injects `camera_id` into each event |
| 9b iter 3 — Per-camera disk layout | Snapshots `/data/snapshots/{camera_id}/`, vehicle snapshots `/data/snapshots/vehicles/{camera_id}/{date}/`, event journal entries tagged with camera; retention pruner walks subdirs; `routes/events.py` adds `resolve_event_snapshot_path()` with legacy-flat fallback |
| 8b iter 2 — Per-camera detail view | `/single.html?camera=X` scopes WebSocket + config sliders + zones + events to that camera; backend `/api/config`, `/api/zones`, `/api/events`, `/api/stats` all accept `?camera=` |
| DVR enabled locally | `recorder` (front_door) + `recorder-cam2` (basement) running by default; 3-day retention; 1h MPEG-TS segments; `/ai.html` DVR tab has per-camera picker (`/api/recordings/cameras` lists what's actually on disk) |
| Ingester auto-reconnect | Wallclock-based: if no decoded frame in 30s, close+reopen `cv2.VideoCapture`. Self-heals after unplug within ~1 min |
| Recordings → bind mount | `./data/recordings/` on host (was `vision-labs_qnap-recordings` named volume). Browseable from Windows Explorer without sudo |

**🔜 Next up (in priority order):**

1. **Phase 7d — Auto-discovery** (~few hours)
   - ONVIF discovery for IP cameras
   - mDNS for Pi-style streamers

2. **Phase 8 — TV dashboard** (~few hours)
   - `/tv.html` with 10-foot UI

3. **Phase 9 — HomeKit** (later — Homebridge container as a new compose service)

4. **Other bind mounts** — if you want `/data/snapshots`, `/data/events`, `/data/telegram` etc. also bind-mounted to `./data/...` for Windows browsability, same pattern as recordings (copy → swap mount in compose → remove named volume).

### Known small loose ends (none blocking)

- **cam3/cam4 slots not yet in `docker-compose.yml`.** To add: copy the cam2 service block (5 services: ingester, pose-detector, vehicle-detector, tracker, recorder) and rename. Append `"cam3"` / `"cam4"` to `AVAILABLE_SLOTS` in `cameras.py`.
- **`/api/login-bg` is hardcoded to primary camera.** Low priority cosmetic — login background image always pulls from `frame_hd:front_door` regardless of multi-camera state.
- **PHASES.md Tier 2 bugs** still pending:
  - Vehicle stationarity reference never resets (`tracker.py:215-228`) — a parked car nudged 31px is "non-stationary forever" until next idle timeout.
  - GPU pause race: dashboard unloads Ollama models *before* setting `gpu:generation_active` flag → detectors may have inference mid-flight during ComfyUI load.
  - `pose-detector` schema defensiveness: only reads `data[b"frame"]` (vs vehicle-detector which also checks `frame_bytes`). KeyError if upstream changes field name.
- **`faces.js` enrollment wizard** has not been audited for `?camera=` parameter handling. It works for primary; may use primary's frame for enrollment regardless of selected camera. Faces themselves are a global DB so this matters less than it sounds.

### Decision log (so we don't re-debate)

- **Auto-spawn cameras via Docker socket**: deferred. Slot-based + manual `docker compose --profile camN up -d` is the current model.
- **Grid as the home page**: chosen. Old single-camera dashboard moved to `/single.html`.
- **Conditions + Known Faces on home page**: chosen — these are global, not per-camera. **Recent Activity** also lives on the home page as the aggregate feed.
- **Per-camera Settings + Events + Zones**: live in `/single.html?camera=X` (drill-in).
- **Auth redirect**: 303 instead of 307 (more universally followed).
- **Recordings storage**: bind-mounted local disk (3-day retention) until QNAP arrives. Same compose interface works when QNAP is added (just swap mount in compose).
- **Aggregate vs scoped feed semantics**: home page = aggregate ALL cameras with camera badges; single.html = scoped to one camera (no badges). Telegram `/events` no-arg = aggregate; `/events basement` = scoped.
- **Event snapshot storage**: per-camera subdirs `/data/snapshots/{camera_id}/`. Legacy flat `/data/snapshots/*.jpg` still readable via `resolve_event_snapshot_path()` for older events.
- **Camera picker UX in Telegram**: bare `/snapshot` or `/clip` (no camera token + >1 camera) replies with inline-keyboard tap-to-pick buttons. With camera token, runs directly. `/clip 10` (duration but no camera) → picker preserves the 10s through callback_data.

### How to verify state after a session restart

```bash
cd ~/projects/vision-labs
docker compose --profile cam2 ps                   # ~20 services up (base + cam2 slot)
docker compose exec -T redis redis-cli HGETALL cameras:registry   # both cameras
ls data/recordings/                                # front_door/  cam2/ (per-camera dirs)
ls data/recordings/front_door/                     # date dirs YYYY-MM-DD
git log --oneline | head -5                        # last commit should be ed2c3c2 or later
curl -ks -o /dev/null -w "%{http_code}\n" http://localhost:8080/   # 303 (redirect to login)
docker compose logs --tail=5 recorder recorder-cam2 | grep "ffmpeg started"   # both recorders writing
```

---

---

## Why we're doing this

- `server.py` is 1200 lines — auth, middleware, retention, event poller, Ollama warmup, ComfyUI cleanup, and the entire WebSocket loop all live in one file. Hard to reason about, hard to test.
- Some real bugs hide as "dead branches" (e.g. `metrics.py` reads `state.persons` but tracker writes `state.people` — metrics has been showing 0 active persons silently).
- Hardcoded values (`qwen3:14b`, `"zillah.safetensors"`, `MAX_UNKNOWN_FACES=100`) are scattered across files.
- `CAMERA_ID` is read as env-once in 8 places; multi-camera will require this to become a list/registry.
- No central place to find "what does the dashboard do at startup".

---

## What this plan does NOT do

- ❌ Multi-camera *implementation* (separate phase later — but this refactor lays the foundation)
- ❌ HomeKit / TV dashboard (later phase)
- ❌ Behavior changes (no auth tweaks, no bbox logic changes, no new features)
- ❌ Algorithm changes (no IoU thresholds moved, no debounce changes)
- ❌ Dependency upgrades

---

## File sizes (May 10, 2026)

These are all worth knowing because the refactor strategy depends on whether a file is "single concern, just big" or "multiple concerns tangled":

| File | Lines | Concerns |
|------|-------|----------|
| `services/dashboard/routes/bot_commands.py` | **1411** | Polling, 15 commands, photo handler, audit log, role check — split candidate |
| `services/dashboard/server.py` | 1313 | Imports + middleware + 5 pollers + WebSocket — main split target |
| `services/dashboard/routes/ai_tools.py` | 1264 | 18 LLM tool fns — could split by category |
| `services/dashboard/routes/image_gen.py` | 1131 | ComfyUI proxy + workflow + GPU lock + gallery |
| `services/tracker/tracker.py` | 1004 | PersonTracker + VehicleTracker + main loop |
| `services/dashboard/routes/notifications.py` | 932 | Telegram + scene analysis + bbox drawing + rate limit |
| `services/face-recognizer/recognizer.py` | 760 | Acceptable for now |
| `services/face-recognizer/face_db.py` | 462 | Fine |
| `services/dashboard/routes/ai.py` | 445 | Fine |
| `services/pose-detector/detector.py` | 409 | Fine |
| `services/camera-ingester/ingester.py` | 399 | Fine |
| `services/vehicle-detector/detector.py` | 358 | Fine |
| `services/dashboard/routes/metrics.py` | 334 | Fine (also has the bug we're fixing) |
| `services/dashboard/routes/auth.py` | 320 | Fine |

Primary refactor target = `server.py`. Secondary targets = `bot_commands.py`, `ai_tools.py`, `image_gen.py`, `notifications.py`. Tertiary = `tracker.py`.

## Cross-import dependency graph in `routes/`

These exist as **lazy imports inside functions** to avoid circular dependencies:

```
bot_commands.py ──→ notifications.py (5 places)
bot_commands.py ──→ ai_tools.py, ai_prompts.py
ai_tools.py ──→ notifications.py (4 places)
ai.py ──→ ai_state, ai_tools, ai_prompts, image_gen
faces.py ──→ notifications.py
notifications.py ──→ metrics.py
```

**Implication for refactor:** keep the lazy-import pattern. Splitting `bot_commands.py` is safe because its lazy imports don't change. Splitting `notifications.py` requires careful re-exports.

## Phase 0 — Pre-flight inventory (no code changes)

Already done while writing this plan. Recording it here so we don't rediscover later.

### server.py top-level structure (1200 lines, May 2026)

| Lines | Component | What it does |
|-------|-----------|--------------|
| 1-66 | Imports + module setup | imports, logging, httpx logger silenced |
| 68-83 | `_bbox_iou()` | Helper — used by WebSocket only |
| 86-110 | `_in_dead_zone()` | Helper — used by WebSocket only |
| 112-145 | Config constants | CAMERA_ID, REDIS_HOST, stream keys, DEFAULT_CONFIG |
| 149-159 | Logging setup | basicConfig + httpx silence |
| 160-205 | App setup | FastAPI, Redis clients, routes context injection |
| 206-260 | Auth middleware | session cookie validation, route exemption |
| 273-309 | `/api/login-bg` endpoint | blurred camera snapshot for login page |
| 311-356 | `startup()` event handler | init DBs, kick off background tasks |
| 358-396 | `_reminder_poller()` | check due reminders every 60s |
| 398-458 | `_ensure_ollama_model()` | pull qwen3:14b on first startup |
| 460-498 | `_clear_comfyui_queue_on_startup()` | clear stale GPU locks |
| 500-582 | `_retention_poller()` | daily prune of /data/snapshots and /data/events |
| 584-866 | `_event_notification_poller()` | poll events, save snapshots, send Telegram |
| 868-1180 | `websocket_live()` | live frame stream with overlays (the giant one) |

### Identified dead code / bugs

| Location | Issue | Severity |
|----------|-------|----------|
| `routes/metrics.py:128` | `state.get("persons", [])` — tracker writes `"people"`, this always returns `[]`. Active person count is silently always 0 in Prometheus | **Real bug** |
| `routes/metrics.py:306` | Same bug, same fix | **Real bug** |
| `server.py:1027` | `state.get("persons", state.get("people", "[]"))` — works because of fallback, but `persons` branch is dead | Dead branch |
| `tracker.py:31` | Docstring says "Phase 5 adds face-based re-identification" — face recog is now a separate service | Stale comment |
| `contracts/streams.py:11-14` | Docstring mentions "rule engine (Phase 4)" service that never existed | Stale comment |
| `contracts/streams.py:43` comment | "Consumed by: rule engine, dashboard event feed, archive worker" — only dashboard exists | Stale comment |

### Identified hardcoded values that should be config

| Location | Value | Should be |
|----------|-------|-----------|
| `routes/ai.py:53` | `OLLAMA_MODEL = "qwen3:14b"` | `OLLAMA_CHAT_MODEL` env, default `qwen3:14b` |
| `routes/bot_commands.py:1193` | Same hardcoded `qwen3:14b` | Same env |
| `server.py:395` | Same hardcoded `qwen3:14b` (in warmup) | Same env |
| `routes/image_gen.py:135` | `"zillah.safetensors"` default checkpoint | env `DEFAULT_COMFYUI_CHECKPOINT` or first-found in dir |
| `face_db.py:34` | `MAX_UNKNOWN_FACES = 100` | env var |
| `face_db.py:38` | `UNKNOWN_DEDUP_THRESHOLD = 0.6` | env var |
| `keep_alive="5m"` | Ollama keep-alive in 5+ call sites | one constant |

### Tests inventory

`tests/` directory has 9 files:
- `test_actions.py`
- `test_face_db.py`
- `test_feedback_db.py`
- `test_notifications.py`
- `test_routes.py`
- `test_scene_analysis.py`
- `test_time_rules.py`
- `test_tracker.py`
- `test_vehicles.py`

Status: **pytest not installed in the WSL environment**. Can run them inside the dashboard container (or install pytest locally) before/after each refactor step.

---

## Phase 1 — Surgical bug fixes (small, isolated, do before refactor)

These are pure fixes, not refactors. Doing them first ensures the refactor isn't entangled with bug fixes.

- [ ] **Fix `routes/metrics.py:128`** — change `state.get("persons", [])` to `state.get("people", [])`. Same fix on line 306. This is the real bug, not a stylistic one. Validates: `curl /api/metrics | grep vl_active_persons` should show a non-zero number when someone is in frame.
- [ ] **Fix the `server.py:1027` dead branch** — remove the `state.get("persons", ...)` fallback; just `state.get("people", "[]")`. Behavior identical.
- [ ] **Clean stale docstrings**: `tracker.py:31`, `contracts/streams.py:11-14, 43`. No code change.

**Exit criterion:** Prometheus shows active persons = actual count; `docker compose logs` shows no new errors; all services still up.

---

## Phase 2 — Constants module (new file, no extraction risk)

- [ ] Create `services/dashboard/constants.py` with all hardcoded model names, ports, defaults that currently live as literals:
  ```python
  # Ollama
  CHAT_MODEL = os.getenv("OLLAMA_CHAT_MODEL", "qwen3:14b")
  VISION_MODEL = os.getenv("OLLAMA_VISION_MODEL", "minicpm-v")
  OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "5m")
  
  # ComfyUI
  DEFAULT_CHECKPOINT = os.getenv("DEFAULT_COMFYUI_CHECKPOINT", "")
  
  # Face DB
  MAX_UNKNOWN_FACES = int(os.getenv("MAX_UNKNOWN_FACES", "100"))
  UNKNOWN_DEDUP_THRESHOLD = float(os.getenv("UNKNOWN_DEDUP_THRESHOLD", "0.6"))
  ```
- [ ] Replace all 3 hardcoded `qwen3:14b` references with `from constants import CHAT_MODEL`.
- [ ] Replace `keep_alive="5m"` strings (5 sites) with `from constants import OLLAMA_KEEP_ALIVE`.
- [ ] Update `face_db.py` to read its two constants from env (same constants module pattern, scoped to face-recognizer service).

**Exit criterion:** `grep -r "qwen3:14b" services/dashboard/` returns nothing except the constants file; AI chat still works (`POST /api/ai/chat` round-trips).

---

## Phase 3 — Helper module (low risk)

- [ ] Create `services/dashboard/helpers/geometry.py` with `_bbox_iou` and `_in_dead_zone`. Pure functions, no side effects.
- [ ] Update `server.py` to `from helpers.geometry import bbox_iou, in_dead_zone` (drop the leading underscore — they're now public to the module).
- [ ] Verify with the test suite: `tests/test_tracker.py` and `tests/test_routes.py` should still pass.

**Exit criterion:** server.py drops ~50 lines; WebSocket overlays still draw correctly in browser.

---

## Phase 4 — Extract pollers (medium risk — one at a time)

The pattern: each poller becomes its own file under `services/dashboard/pollers/`. The async function is imported by `server.py:startup()` and scheduled the same way it is today.

### 4.1 — `pollers/reminders.py` (smallest, simplest, safest first)
- [ ] Create `services/dashboard/pollers/__init__.py` (empty).
- [ ] Create `pollers/reminders.py` with `reminder_poller(ai_db)` (renamed without leading underscore).
- [ ] Move the body of `_reminder_poller()` from server.py:358-396.
- [ ] Server.py imports it and schedules it.
- [ ] **Rebuild dashboard. Verify: dashboard starts cleanly, log line shows reminder poller scheduled.**

### 4.2 — `pollers/ollama_warmup.py`
- [ ] Move `_ensure_ollama_model` (server.py:398-458) → `pollers/ollama_warmup.py:warm_ollama()`.
- [ ] Server.py imports + schedules.
- [ ] **Rebuild. Verify: log shows "AI model ... downloaded successfully" or "model already exists".**

### 4.3 — `pollers/comfyui_cleanup.py`
- [ ] Move `_clear_comfyui_queue_on_startup` (server.py:460-498) → `pollers/comfyui_cleanup.py:clear_comfyui_queue()`.
- [ ] Server.py imports + schedules.
- [ ] **Rebuild. Verify: log shows "Startup: cleared GPU pause flag and stale generation lock".**

### 4.4 — `pollers/retention.py`
- [ ] Move `_retention_poller` (server.py:500-582) → `pollers/retention.py:retention_poller()`.
- [ ] Server.py imports + schedules.
- [ ] While we're here, add the missing INFO log at startup so liveness is observable:
  `logger.info(f"Local retention enabled (retention_days={...})")`
- [ ] **Rebuild. Verify: log shows retention startup line.**

### 4.5 — `pollers/events.py` (the big one — ~280 lines)
- [ ] Move `_event_notification_poller` (server.py:584-866) → `pollers/events.py:event_notification_poller()`.
- [ ] This one has nested functions (`_journal_event`, `_save_snapshot`) — keep them as inner functions or move to module level.
- [ ] Identify the module-level vars it touches: `r`, `r_bin`, `logger`, `CAMERA_ID`, `EVENT_STREAM`, `HD_FRAME_KEY`. All available via the `routes.ctx` pattern.
- [ ] Server.py imports + schedules.
- [ ] **Rebuild. Verify:**
  - Dashboard starts cleanly
  - Walk in front of camera → Telegram notification arrives
  - Event appears in `/api/events` and `/data/events/<date>.jsonl`
  - Snapshot saved to `/data/snapshots/<id>.jpg`

**Exit criterion:** server.py is ~600 lines (down from 1200), pollers each in their own file, no behavior change.

---

## Phase 5 — Extract WebSocket loop (highest risk — most coupling)

- [ ] Create `services/dashboard/websocket.py` with `register(app)` function.
- [ ] Move `@app.websocket("/ws/live")` and `websocket_live()` (server.py:868-1180) entirely.
- [ ] Pay close attention to:
  - The function-attribute caches (`websocket_live._sticky_identities`, `_zone_cache`) — these should become module-level dicts or, better, per-connection (this is the long-standing Tier 2 bug, finally fixable here).
  - The `_read_target_fps` closure — keep it local.
  - References to `r`, `r_bin`, `logger`, all the stream key constants.
- [ ] Decision point: **should we also fix the sticky-identity per-connection bug here?** It's natural while moving the code. PHASES.md Tier 2 lists this. Recommend: yes, move to per-connection dict at the same time — costs nothing extra, removes a known bug.
- [ ] Server.py calls `websocket.register(app)` after `app = FastAPI(...)` and routes are mounted.
- [ ] **Rebuild. Verify:**
  - Browser sees live frames
  - Bboxes draw correctly
  - Two browser tabs don't corrupt each other's face labels (would fail today, would pass after the per-connection fix)
  - WS still rejects no-cookie connections with 4401

**Exit criterion:** server.py is ~250 lines (down from 600); WebSocket logic is in its own file; sticky-identity bug fixed as a free side effect.

---

## Phase 6 — Final shape of server.py (~200-300 lines)

After phases 1-5, server.py should contain only:
- Imports
- FastAPI app instance + Redis clients
- Route context injection (existing pattern)
- Auth middleware
- `/api/login-bg` endpoint (small, login-page-specific; doesn't need to move)
- `startup()` event handler that imports and schedules pollers, registers WebSocket
- Static file mount

Goal: a new dev can read server.py in 5 minutes and know what runs at startup, where each subsystem lives.

---

## Phase 7 — Multi-camera scaffolding (still in this refactor pass)

These are pure structural changes that don't *enable* multi-camera but make adding it later trivial.

### Architecture decision: service-per-camera (recommended for 2-4 cameras)

**Why this and not multi-camera-aware services:**

| Pattern | Pros | Cons | When |
|---------|------|------|------|
| **Service-per-camera** | Trivial code change; isolation; one camera crashing doesn't kill others | More container instances; ~4 GB VRAM per detector × N cameras | Recommended for 2-4 cameras |
| Multi-camera-aware services | One process per role; better GPU sharing | 1-2 days refactor; tracker becomes complex; single point of failure | 5+ cameras |

Your 5070 Ti has 16 GB; pose + vehicle + face for ONE camera use ~4 GB. So 3-4 cameras is the upper limit for service-per-camera before you'd want to consolidate. Plenty of room.

### Service-per-camera implementation pattern

Each camera = a copy of these services in compose: `camera-ingester-<id>`, `pose-detector-<id>`, `vehicle-detector-<id>`, `tracker-<id>`. The remaining services (face-recognizer, dashboard, ollama, comfyui, recorder if NAS on) are shared.

In compose this becomes too verbose to maintain by hand. Two options:

**Option 1 (clean):** Docker Compose YAML anchors + per-camera override file
```yaml
# In docker-compose.yml — define a base anchor
x-detector-base: &detector-base
  build:
    context: ./services/pose-detector
  environment: &detector-env
    REDIS_HOST: redis
  deploy: &gpu0
    resources:
      reservations:
        devices:
          - driver: nvidia
            device_ids: ['0']
            capabilities: [gpu]

# Then per-camera concrete services
pose-detector-front_door:
  <<: *detector-base
  environment:
    <<: *detector-env
    CAMERA_ID: front_door

pose-detector-backyard:
  <<: *detector-base
  environment:
    <<: *detector-env
    CAMERA_ID: backyard
```

**Option 2 (cleaner long-term):** a small Python script `scripts/generate-compose.py` that reads `cameras:registry` from Redis (or a local YAML config) and emits `docker-compose.cameras.yml`.

I lean toward Option 2 for >2 cameras. We don't have to implement it now — Phase 7 just lays the groundwork.

### Phase 7 checklist

- [ ] Create `services/dashboard/cameras.py` with a `CameraRegistry` class backed by Redis (`cameras:registry` hash). Entries: `{id, name, rtsp_main, rtsp_sub, location_lat, location_lon, gpu_id, enabled}`.
- [ ] Default seed: on first startup, if registry is empty, seed `front_door` from env vars so today's deployment works unchanged.
- [ ] Add read-only endpoints in `routes/cameras.py`:
  - `GET /api/cameras` → list
  - `GET /api/cameras/{id}` → details
  - `GET /api/cameras/{id}/state` → current scene snapshot for that camera
- [ ] Add admin endpoints (auth required):
  - `POST /api/cameras` → register a new camera (does NOT spawn services; that's Phase 7b)
  - `PUT /api/cameras/{id}` → update config
  - `DELETE /api/cameras/{id}` → unregister
- [ ] WebSocket gains optional `?camera=<id>` query param. If missing, defaults to env `CAMERA_ID` (today's behavior).
- [ ] All existing endpoints continue working unchanged — registry is purely additive.

**Exit criterion:** `curl /api/cameras` returns `[{"id":"front_door", ...}]`. Single-camera behavior unchanged.

### Phase 7b — Actually adding a second camera (future, AFTER 7)

When ready to add camera #2:

1. Set up the new RTSP camera (LAN access, RTSP URL, credentials).
2. Add via API: `POST /api/cameras {"id":"backyard","rtsp_sub":"rtsp://...","rtsp_main":"rtsp://...",...}`.
3. Generate the per-camera compose entries (manually or via the generator script).
4. `docker compose up -d camera-ingester-backyard pose-detector-backyard ...`.
5. Dashboard UI camera switcher picks it up from registry.

Estimated time: 30 min once Phase 7 is done.

---

## Phase 8b — Multi-camera grid view in the main dashboard (concrete design)

Independent of the TV dashboard (Phase 8). This is for the *operator* dashboard at `index.html`.

### Design

- Camera tiles laid out in a grid on the live-view page
- Each tile shows a small live feed + bbox overlays + status badge
- **Resize:** drag corner of any tile (CSS grid + JS resize handlers)
- **Move:** drag tile header to reorder positions
- **Click:** opens that camera in a full-screen modal with full overlays, event feed, zone editor, all the current single-camera dashboard features
- **Layout persists** per-user in `localStorage` or in Redis under `user_prefs:{username}:dashboard`

### Implementation sketch

- New JS module `static/dashboard-grid.js` that wraps multiple WebSocket clients (one per camera)
- Each tile is a self-contained DOM element with its own WebSocket connection
- A "camera switcher" in the existing live view becomes a "grid <-> single" mode toggle
- Modal view reuses the existing single-camera view code

### Effort

~1-2 days. Doesn't need any backend changes — purely a frontend rebuild of the live view. Best done after Phase 8 (TV dashboard) since they share the "multiple WS connections at once" infrastructure.

---

## Phase 8 — TV viewing dashboard (concrete design)

A dedicated UI optimized for 10-foot viewing (Apple TV / Fire TV / Chromecast / browser on the TV). Not a separate service — just a new static page in the dashboard.

### Design

- **URL:** `/tv.html`
- **Auth:** still requires session cookie; on the TV browser, log in once and it sticks.
- **Layout options** (query params):
  - `/tv.html?layout=single&camera=front_door` — full-screen one camera
  - `/tv.html?layout=2x2` — 4 cameras in a grid (uses registry)
  - `/tv.html?layout=3x3` — 9 cameras
- **Overlay info per tile:**
  - Camera name
  - Live FPS, persons-in-frame count
  - Latest event banner (slides in for 10s when a person/vehicle event fires)
  - No buttons, no settings — passive viewing only

### Implementation

- [ ] New static file `services/dashboard/static/tv.html`.
- [ ] Pull WebSocket logic out into a reusable JS module: `static/lib/camera-stream.js` — used by both `index.html` and `tv.html`.
- [ ] Add `routes/tv.py` if any TV-specific endpoints needed (event banner subscription, etc. — but WebSocket should cover it).
- [ ] CSS uses `vw`/`vh` units so it scales for any TV resolution.

Estimated effort: 1-2 days. Doesn't require multi-camera to exist — single-camera TV view is meaningful too.

---

## Phase 9 — HomeKit integration (concrete design)

HomeKit support gives you:
- Cameras visible in Apple Home app
- Person/vehicle detection events as motion triggers
- Snapshots in the Home app
- Siri shortcuts ("Hey Siri, show front door")
- Apple TV displays cameras automatically when motion fires

### Architecture choices

**Option A: Use existing Homebridge** (recommended)
- Run `homebridge/homebridge:latest` in compose as a new service
- Use the **`homebridge-camera-ffmpeg`** plugin pointing to your RTSP URLs (Reolink directly)
- Use **`homebridge-mqtt-thing`** or **`homebridge-http-switch`** to expose dashboard events
- Pros: mature ecosystem, no custom HomeKit code, lots of plugins
- Cons: bridges through Homebridge config UI; less integrated

**Option B: Custom HAP-Python bridge** (deeper integration)
- New service `services/homekit-bridge/`
- Uses `hap-python` library (no Homebridge)
- Subscribes to events from Redis directly
- Exposes Camera + MotionSensor + Switch accessories
- Pros: full control, tighter integration, no extra UI
- Cons: ~2-3 days work, must maintain ourselves

### Phase 9 checklist (using Option A initially)

- [ ] Add `homebridge` service to compose. Profile-gated (`--profile homekit`).
- [ ] Mount config volume.
- [ ] Configure `homebridge-camera-ffmpeg` to use the camera RTSP URLs from the camera registry.
- [ ] Add an `MQTT` motion event publisher to the dashboard event poller — pushes person/vehicle events.
- [ ] In Homebridge config, set up `homebridge-mqtt-thing` to translate MQTT motion → MotionSensor accessory.
- [ ] Pair with iPhone / iPad / Apple TV via Home app.

Estimated effort: 1 day.

### Migration path to Option B (later, if needed)

- Replace `homebridge` service with `services/homekit-bridge/` using `hap-python`.
- All HomeKit pairing data persists in the bridge's own config dir.
- Direct Redis stream subscription instead of MQTT relay.

---

## Architecture summary diagram (post-refactor + multi-camera + TV/HomeKit)

```
                       ┌─────────────────────────────┐
                       │       Redis (shared)        │
                       │  Streams + state + config   │
                       └─────────────────────────────┘
                                    ▲
       ┌────────────────────────────┼─────────────────────────────┐
       │                            │                             │
       ▼                            ▼                             ▼
  Per-camera services        Shared services            Integration services
  ─────────────────────      ───────────────────        ─────────────────────
  camera-ingester-XX         face-recognizer            homekit-bridge
  pose-detector-XX           dashboard (HTTP+WS)        (publishes motion to
  vehicle-detector-XX        ollama                      HomeKit via MQTT)
  tracker-XX                 comfyui
  (recorder-XX if NAS)       prometheus
                             grafana                    Apple Home / TV
                             portainer                  pairs with bridge
                                                          ▲
                             ▲                            │
                             │ HTTPS, cookie auth         │
                             │                            │
                       ┌─────┴───────┐               iOS / Apple TV
                       │   Browser   │               (Home app)
                       │  index.html │
                       │  tv.html    │
                       │  ai.html    │
                       └─────────────┘
```

---

## Phase 9 — Cleanup pass after refactor

- [ ] Remove unused imports across moved files.
- [ ] Update `ARCHITECTURE.md` to reflect new file layout.
- [ ] Update `PHASES.md`: mark sticky-identity bug fixed (resolved as part of phase 5), update Phase 5 tier items.
- [ ] Run all the smoke tests one more time end-to-end:
  - Dashboard live view (browser)
  - Walk in front of camera → bbox + Telegram alert
  - AI chat round-trip
  - Vision analysis (minicpm-v)
  - Settings slider change persists
  - Logout / login flow

---

## How we know we didn't break anything

After every checkbox we tick, the validation checklist is:

1. `python3 -c "import ast; ast.parse(open(...).read())"` on every edited file — syntax sanity.
2. `docker compose build dashboard` — builds clean.
3. `docker compose up -d dashboard` — starts clean.
4. `docker compose logs --tail=50 dashboard | grep -iE "error|exception|traceback"` — no new errors.
5. `curl -ks http://localhost:8080/api/auth/status` returns HTTP 200.
6. WebSocket smoke test (a quick python-websockets connection with valid cookie should still get frames).
7. (Bigger phases only) browser-level smoke: log in, see live view, see events.

If any step breaks, we revert the last change and figure out why before continuing.

---

## Status tracker

| Phase | Status | Owner | Notes |
|-------|--------|-------|-------|
| 0 — Inventory | ✅ done | — | This document |
| 1 — Bug fixes | ✅ done (`0536d39`) | claude | metrics.py was using r.get() on a hash + wrong key. Verified: vl_active_persons now reflects num_people |
| 2 — Constants module | ✅ done (`e0e93ee`) | claude | services/dashboard/constants.py created; 13 literals removed; needed Dockerfile COPY constants.py |
| 3 — Helper module | ✅ done (`3b2a767`) | claude | helpers/geometry.py — bbox_iou + in_dead_zone. server.py -41 net lines |
| 4.1 — Reminder poller | ✅ done (`8e45147`) | claude | First extraction; pattern validated |
| 4.2 — Ollama warmup | ✅ done (`7763166`) | claude | |
| 4.3 — ComfyUI cleanup | ✅ done (`3655c52`) | claude | Uses constants.COMFYUI_HOST |
| 4.4 — Retention | ✅ done (`8948755`) | claude | Liveness log added at startup |
| 4.5 — Event poller | ✅ done (`eee1b4e`) | claude | 280 lines moved cleanly; nested fns preserved |
| 5 — WebSocket | ✅ done (`3dc24c2`) | claude | 430 lines moved; sticky-identity bug fixed (now per-connection); verified live browser still works |
| 6 — server.py shape | ✅ done | — | **344 lines** (74% smaller than baseline 1313). Pure wiring file now. |
| 7 — Camera registry | ✅ done | claude | cameras.py + routes/cameras.py; seeded front_door from env on first boot |
| 7b — Camera management UI | ✅ done (`8aa110e`) | claude | cameras.html admin page + test-rtsp ffprobe endpoint |
| 7c — Slot-based per-camera services | ✅ done (cam2; cam3/cam4 = copy-paste later) (`ad0e1be`) | claude | cam2 slot ready; ingester reads RTSP from registry; detectors honor detect_X flags |
| 7d — Auto-discovery (ONVIF + Pi mDNS) | ⏸️ later | claude | Nice-to-have on top of 7c |
| 7e — Auto-spawn via Docker socket | ⏸️ deferred (intentionally) | — | Mount /var/run/docker.sock in dashboard, spawn containers automatically on Save. Cleaner UX but adds attack surface. See decision log. |
| 8b iter 1 — Multi-cam grid view (home) | ✅ done (`eba1d36` + `c9f95f5` + `4f2adb7`) | claude | grid view at `/`; modal expand; mobile-responsive; conditions + faces panels below |
| 8b iter 2 — Parameterize single-camera view by ?camera=X | ⬜ pending | claude | `app.js`, `events.js`, `zones.js`, `faces.js` read URL param + pass to backend |
| 8b iter 3 — Full sidebar in grid modal | ⬜ future | claude | Less needed once iter 2 is done |
| 9a iter 1 — AI multi-camera (3 tools) | ✅ done (`667f0ab`) | claude | `get_live_scene` aggregates; `query_events`/`capture_snapshot` take `camera` arg; system prompt lists cameras |
| 9a iter 2 — Remaining 7 AI tools | ⬜ pending | claude | events_by_date, zones, browse_vehicles, event_patterns, activity_heatmap, capture_clip, get_system_status |
| 9b — Telegram bot multi-camera | ⬜ pending | claude | `/snapshot [camera]`, `/clip [N] [camera]`, etc. |
| 8 — TV dashboard | ⬜ future | claude | `/tv.html` — works with 1 camera too |
| 9 — HomeKit (Homebridge) | ⬜ future | claude | Easier first iteration |
| 9b-internal — HomeKit (HAP-python) | ⏸️ future | — | If we outgrow Homebridge |
| 10 — Cleanup | ⬜ blocked-by-others | — | Final pass |

**Phases 1-7 are the actual refactor.** Phases 7b-10 are future features that benefit from the refactor being done.

---

## Post-refactor follow-ups discovered in the wild

### tracker `block=0` deadlock on `detect_vehicles=false` cameras *(fixed May 2026)*

**Symptom:** `tracker-cam2` looked alive (container Up, banner logged, SIGTERM handled cleanly) but `events:cam2` stayed at 0 and `state:cam2` was empty for 8 hours. `/who basement` Telegram returned "no state". Pose detector for cam2 was producing detections normally (`detections:pose:cam2` had 1000+ entries) and face-recognizer-cam2 was consuming fine.

**Root cause:** `services/tracker/tracker.py` had two `XREADGROUP` calls per loop iteration — one for pose, one for vehicles. The vehicle call used `block=0` with a comment `# Non-blocking — just check what's available`. **The comment was wrong.** In Redis Streams, `block=0` means "block indefinitely". For front_door this was fine because `vehicle-detector` (single-instance, GPU 0) constantly publishes to `detections:vehicle:front_door`, so the call returns within milliseconds. For cam2, the camera registry has `detect_vehicles: false` and there is no `vehicle-detector-cam2` service, so `detections:vehicle:cam2` is permanently empty — the call **blocked forever** on the very first iteration, before the pose message could be processed/acked.

**Diagnosis trail:**
- `XINFO CONSUMERS detections:pose:cam2 trackers` → `idle: 274000 ms`, `pending: 1` → consumer made one read and stopped
- `XPENDING` → the one pending message ID matched `last-delivered-id` → tracker received message but never acked
- `py-spy dump --pid 1` inside container → stack stuck in `redis._read_from_socket` inside `xreadgroup` (proved it was inside a syscall, not crashed)

**Fix:** remove the `block=` argument from the vehicle XREADGROUP call. With redis-py, omitting `block` means no BLOCK option in the Redis command, which translates to "return immediately if no data" — the intended behavior.

**Secondary gotcha during the fix:** docker-compose builds **per-service images**, not per-build-context. `docker compose build tracker` only rebuilt `vision-labs-tracker:latest`. `tracker-cam2` uses a separate image tag `vision-labs-tracker-cam2:latest` that didn't get rebuilt until `docker compose build tracker-cam2` was run explicitly. The cam2 container was running stale code with the old `block=0` until that second build. Watch for this with any future per-camera service rebuild — `docker compose build` with no service arg rebuilds everything safely.

### Telegram bbox offset *(open as of May 2026 — cosmetic; diagnosis below, fix held for verification)*

**Observed:** On cam2 person-detection Telegram photos the bounding box draws offset to the right of (and slightly below) the actual person, even with a stationary subject. User suspects the same offset exists on front_door — needs side-by-side verification tomorrow.

#### Tentative diagnosis (traced May 11, ~04:30 EDT — independent re-trace planned tomorrow)

**Most-likely root cause:** `services/dashboard/routes/notifications.py:392-402` `draw_bbox_on_frame()`:

```python
# If snapshot is HD (>= 1000px wide), scale bbox from SD coords
if snap_w >= 1000:
    sd_frame = get_sd_frame()      # ← Bug A: no camera_id arg → returns primary camera's frame
    if sd_frame:
        sd_arr = np.frombuffer(sd_frame, np.uint8)
        sd_img = cv2.imdecode(sd_arr, cv2.IMREAD_COLOR)
        if sd_img is not None:
            sd_h, sd_w = sd_img.shape[:2]
            sx = snap_w / sd_w
            sy = snap_h / sd_h
            x1, y1, x2, y2 = x1 * sx, y1 * sy, x2 * sx, y2 * sy
```

Two compounding issues:

1. **Heuristic `snap_w >= 1000`** assumes only HD main-streams have ≥1000-wide frames. Basement camera's RTSP **sub-stream is 1280×720** (verified in `camera-ingester-cam2` startup log) — it trips the threshold even though it's a sub-stream, so the code mistakenly applies HD-rescaling.

2. **`get_sd_frame()` called with no `camera_id` arg** (helper defined at notifications.py:351). The fallback path uses `ctx.FRAME_STREAM`, which is bound to the dashboard's primary camera = front_door. So for cam2 events the "SD reference frame" used to compute the scale factor comes from the wrong camera entirely (front_door's 896×512 sub-stream).

**Predicted scaling for cam2** with a `[188, 197, 473, 714]` bbox (from a real recent event):
- `snap_w` = 1280 → triggers HD branch
- `sd_frame` = front_door 896×512 (wrong camera)
- `sx` = 1280 / 896 ≈ 1.428
- `sy` = 720 / 512 ≈ 1.406
- scaled bbox: `[269, 277, 676, 1004]` — ~80px right, ~80px down, bottom edge overflows the 720-px frame
- → matches the user's described "to the right of me" offset, direction + rough magnitude.

**Predicted front_door behavior under the same code path:**
- Sub-stream snapshot (896×512): `snap_w < 1000` → no scaling → ✅ correct
- HD snapshot (2304×1296): `snap_w ≥ 1000`, `get_sd_frame()` returns front_door 896×512 (correct camera!) → `sx = 2304/896 ≈ 2.57` → ✅ correct (this is exactly the case the heuristic was designed for)

So this code path **should be correct on front_door** in both snapshot modes. If front_door also shows offset, the bug is in a different overlay path — candidates to check tomorrow:
- `services/dashboard/pollers/events.py:170-202` (uses explicit `is_hd` flag — looks correct on first read; verify)
- `services/dashboard/websocket.py:352-410` (live-overlay path — different bbox source)
- vehicle-event path at `services/dashboard/pollers/events.py:251` (no scaling at all — could be wrong on HD)

**Supporting evidence gathered tonight:**
- ingester logs: cam2 sub = 1280×720; front_door sub = 896×512, HD = 2304×1296
- pose-detector emits `frame_width/frame_height` directly from `frame.shape` after `imdecode` — no inference-time resize that the bbox would need un-mapping from
- tracker stores both `bbox` (latest) and `snapshot_bbox` (frozen at snapshot capture). Notification path uses `snapshot_bbox` first (verified at notifications.py:725, 786, 879), so bbox value is correctly time-aligned with the snapshot — the bug must be in the *draw* step, not the *capture* step.

#### Proposed fixes (not applied — review tomorrow first)

- **Option A (minimal, ~3 lines):** thread `camera_id` through `draw_bbox_on_frame()` to `get_sd_frame(camera_id=...)`. For cam2, the reference becomes its own 1280×720 sub-stream → sx/sy = 1.0 → scaling becomes a no-op even though the heuristic still triggers. Quick to apply, low risk, but leaves the broken heuristic in place.

- **Option B (proper fix):** drop the `>= 1000` heuristic entirely. Either:
  - have the tracker stamp `frame_width/frame_height` into the event hash (the pose-detector already reports them in its detection message), then in `draw_bbox_on_frame` compare to the snapshot's actual `img.shape` and scale only if they differ; or
  - have `get_sd_frame()` always be the reference for a given camera and compute the scale factor unconditionally (1× when sizes match, real factor when they don't).
  Either form removes the foot-gun for any future camera with a wide sub-stream.

- **Option C (bonus, recommended either way):** change `get_sd_frame()` default — if no `camera_id` is passed, raise or warn rather than silently falling back to the primary. Prevents this exact class of bug from recurring elsewhere.

#### Tomorrow's verification plan

1. Walk in front of both cameras, screenshot the Telegram photo for each. Note offset direction + magnitude.
2. **If predictions hold** (cam2 offset, front_door correct): apply Option A as a one-shot test, rebuild dashboard, retest both cameras. Both should align.
3. **If front_door is also offset**: my trace is wrong / incomplete — investigate the events poller and websocket overlay paths instead. The bug is somewhere downstream of `_emit_event` either way, but Option A wouldn't fix it.
4. Independent of which is right — Option B is the cleaner long-term fix once we've confirmed which path is the culprit. Option C is worth doing regardless.

#### Bug-fix discipline reminder

The `block=0` tracker bug had a similarly confident single-trace diagnosis tonight, and the **per-service image gotcha** hid behind it for a full extra rebuild cycle. Treat this trace as a strong hypothesis, not a confirmed root cause, until tomorrow's observation matches the predictions in this section.

---

## Decision log

### Why slot-based (7c) over auto-spawn (7e)

Two viable approaches to "make the camera actually start detecting after Save":

**Slot-based (chosen):**
- Pre-define cam2/cam3/cam4 service slots in `docker-compose.yml`, profile-gated
- Dashboard tells the user `docker compose --profile cam2 up -d` after Save
- No new attack surface
- One terminal command per added camera (mild friction)
- Hard cap on number of cameras unless we expand the slots

**Auto-spawn (deferred):**
- Mount `/var/run/docker.sock` into the dashboard container
- Use the `docker` Python library to spawn containers on Save
- One-click UX, no terminal
- New attack surface: if the dashboard is ever exploited (XSS, dep RCE, etc.), attacker has root on the host
- Acceptable for a LAN-only single-user home setup, but defer until we've actually wanted it for a while

**Decision (May 2026):** Build 7c first. The Docker-socket access is a known-cheap-to-add future feature (~half day) when/if we want it. Forcing one terminal command per camera is acceptable in exchange for not handing root-equivalent access to the web service.

## Rollback strategy

Each phase is one or more commits. If anything goes wrong, `git reset --hard <previous-good-sha>` to the previous good state. The full Phase 1-8 refactor is reflected in this repo's commit history; commit SHAs are immutable so historical rollback points remain valid.
