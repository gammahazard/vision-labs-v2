# Vision Labs — Migration & Re-bootstrap Plan

> Goal: get the stack running cleanly on the new host (WSL2 + Docker Engine, no Docker Desktop) and the new GPUs (1× RTX 3090 + 1× RTX 5070 Ti), then clean up WIP rough edges, then build new features.

Each phase has an **exit criterion** — don't move forward until it's met. Tick boxes as you go.

---

## Phase 0 — Host bootstrap (WSL + Docker Engine + project move)

**Why:** The current project lives at `/mnt/c/...`. Bind mounts on `/mnt/c` are dramatically slower over the 9p protocol, CIFS volumes behave oddly, and you don't actually want Docker Desktop. Fix all of this before touching code.

- [ ] Pick a WSL2 distro (Ubuntu 24.04 LTS recommended). Confirm `wsl --list -v` shows it as version 2.
- [ ] **Uninstall Docker Desktop** if it's still installed. Remove its WSL integration too.
- [ ] Install Docker Engine inside WSL natively (the `get.docker.com` script or Docker's apt repo — *not* the Desktop installer).
- [ ] Install `nvidia-container-toolkit` inside WSL: `sudo apt install nvidia-container-toolkit && sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker`.
- [ ] Verify GPU passthrough: `docker run --rm --gpus all nvidia/cuda:12.8.0-base-ubuntu24.04 nvidia-smi` — should list **both** GPUs.
- [ ] Move the project off `/mnt/c` into WSL's ext4: `mv /mnt/c/Users/adhaliwal/python-projects/vision-labs-v1-main ~/projects/vision-labs && cd ~/projects/vision-labs`. Confirm with `df -T .` that the filesystem is ext4, not 9p.
- [ ] Add a Portainer service to `docker-compose.yml` (or run standalone on port 9443) so you have the web dashboard you liked.
- [ ] **Exit criterion:** `docker compose ps` works in the new path; `nvidia-smi` works inside a CUDA 12.8 container; Portainer UI reachable at `https://localhost:9443`.

---

## Phase 1 — Environment audit (.env + QNAP + secrets)

**Why:** You mentioned new env values to add and uncertainty about whether the QNAP is still alive. We need a single source of truth for configuration before anything else gets debugged.

- [ ] Make a fresh `.env` from `.env.example`. List **every** value you need to add (camera, Telegram, OpenWeather, location, NAS) here in this doc:

```
# Fill in:
CAMERA_IP=
CAMERA_USER=
CAMERA_PASSWORD=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_ALLOWED_USERS=
OPENWEATHER_API_KEY=
LOCATION_NAME=
LOCATION_LAT=
LOCATION_LON=
LOCATION_TIMEZONE=
QNAP_ENABLED=false        # flip to true once NAS is back online
QNAP_IP=                  # leave blank if QNAP_ENABLED=false
QNAP_USER=
QNAP_PASSWORD=
```

- [ ] Probe the QNAP from the WSL host: `ping $QNAP_IP`, `smbclient -L //$QNAP_IP -U $QNAP_USER`. If both fail, mark NAS as dead.
- [ ] **Make NAS storage optional, not required.** Decision: if QNAP is up, save to it; if down, skip persistence entirely (no local fallback). Implementation:
  - Add a `QNAP_ENABLED=true|false` env flag.
  - Gate the seven `qnap-*` CIFS volumes in `docker-compose.yml` behind a Compose `profiles:` (e.g. `profiles: ["nas"]`) so they're only attached when `--profile nas` is used; or use an override file pattern (`docker-compose.qnap.yml`) layered on top of the base compose.
  - In the writers (recorder retention loop, dashboard event poller `_event_notification_poller`, telegram audit, image-gen output, snapshot save), check `QNAP_ENABLED` (or the existence/writability of `/data/<dir>`); if false/unwritable, log once at INFO and skip the write — do **not** write locally as a fallback.
  - Recorder needs its own decision: with no NAS, either disable the recorder service (compose profile) or accept that DVR is off.
- [ ] **If NAS is alive but moved** → just update `QNAP_IP`. CIFS mount will reconnect.
- [ ] **Exit criterion:** stack starts cleanly with `QNAP_ENABLED=false` (no broken mounts, services don't crash on missing `/data/*` paths) **and** with `QNAP_ENABLED=true` once the NAS is back.

---

## Phase 2 — Camera network diagnosis

**Why:** WiFi-antenna setup, no clear picture of subnet/IP. The ingester and recorder use `network_mode: host` and assume direct LAN reachability to `${CAMERA_IP}` over RTSP/TCP. If that's broken, no frames flow and nothing else matters.

Run from PowerShell (Windows host) **and** from WSL — note any difference:

- [ ] `ipconfig` (Windows) / `ip addr` (WSL) — record the host's subnets.
- [ ] `arp -a` — list of devices currently visible on the LAN. Camera should appear here.
- [ ] `ping <camera_ip>` — confirms L3 reachability.
- [ ] `ffprobe -rtsp_transport tcp rtsp://user:pass@<ip>:554/h264Preview_01_sub` (install ffmpeg first) — confirms RTSP works *outside* Docker. **Do this before debugging any container behavior.**

If ping fails:
- Check the WiFi antenna's admin page for the camera's current lease (it likely changed).
- Check whether the antenna is operating as a **bridge** (camera gets a LAN IP) vs **NAT'd extender** (camera is on a sub-subnet and unreachable directly without port-forwarding).
- Set a static DHCP reservation on the camera once you find it.

If RTSP works on host but not in container:
- WSL2's `network_mode: host` semantics differ slightly from native Linux. Verify with `docker run --rm --network=host alpine ping <camera_ip>`.

- [ ] **Exit criterion:** `ffprobe` from inside a host-network Docker container successfully reads 5 seconds of the RTSP sub-stream.

---

## Phase 3 — Hardware enablement (CUDA 12.8 + GPU pinning)

**Why:** The 5070 Ti is Blackwell (sm_120) and needs CUDA 12.8+ runtime. Right now four Dockerfiles disagree on CUDA version (11.8 / 12.1 / 12.2). Also: every GPU service requests `count: 1` with no device pinning, so Docker assigns randomly — fine on identical 3090s, bad on mixed cards.

- [ ] Bump base images to `nvidia/cuda:12.8.0-runtime-ubuntu22.04` (or 24.04) in:
  - [ ] `services/pose-detector/Dockerfile`
  - [ ] `services/vehicle-detector/Dockerfile`
  - [ ] `services/face-recognizer/Dockerfile`
  - [ ] `services/comfyui/Dockerfile`
- [ ] Update Python wheel sources where pinned to a specific CUDA build (PyTorch cu121 → cu128, onnxruntime-gpu 1.18.1 → latest cu128-compatible).
- [ ] Keep `numpy<2` in `face-recognizer/requirements.txt` — InsightFace/onnxruntime is compiled against NumPy 1.x ABI.
- [ ] Pin GPUs explicitly in `docker-compose.yml` using `device_ids` instead of `count: 1`. Suggested split:
  - **GPU 0 (3090, 24 GB)** → `ollama`, `comfyui` (heavy on-demand workloads benefit from headroom)
  - **GPU 1 (5070 Ti, ~16 GB)** → `pose-detector`, `vehicle-detector`, `face-recognizer` (always-on, total <4 GB)
- [ ] Verify `device_ids` order matches `nvidia-smi -L` output (Docker uses PCI bus order; NVML can reorder).
- [ ] **Exit criterion:** each GPU service's logs show "CUDA available" and the expected GPU model (3090 vs 5070 Ti per the split); `nvidia-smi` shows the right processes on the right cards.

---

## Phase 4 — First green run end-to-end

**Why:** Validate the system works as designed before we start "improving" anything.

- [ ] `docker compose up --build` — watch logs for any service that fails to start.
- [ ] Browser hits `http://localhost:8080` → login (default `admin/admin`) → live view shows the camera feed with overlays.
- [ ] Walk in front of the camera → bbox appears, person event fires in the event feed, Telegram notification arrives (if configured).
- [ ] Park a car (or wait for one) → vehicle bbox + idle alert after 90 s.
- [ ] Enroll a face → name sticks to the bbox the next time you appear.
- [ ] AI chat tab → ask "who's in the frame?" → tool-calls `get_live_scene`, replies sensibly.
- [ ] Image gen tab → SDXL generates a test image; detectors show as paused in logs during generation.
- [ ] Recordings tab → at least one `.ts` segment plays back.
- [x] Grafana embed at `monitoring.html` — provisioned with `services/grafana/dashboards/vision-labs.json` (1033 lines, real panels). Working as of audit on 2026-05-10.
- [ ] **Exit criterion:** all of the above works for at least one continuous hour without service restarts.

---

## Phase 5 — WIP code cleanup (now we have a baseline to regress against)

Tackle in roughly this order — each item links to a known issue from the deep-read.

### Tier 1 — Done as part of Phase 0/migration (kept here for reference)

These four were applied during the WSL/Windows migration since they prevent disk-fill, hangs, and obvious UX bugs on first run.

- [x] **Snapshot retention prune** — `server.py` now runs `_retention_poller()` daily that prunes `/data/snapshots/*.jpg`, `/data/snapshots/vehicles/<old-day>/`, and `/data/events/<old-day>.jsonl`. Configurable via `SNAPSHOT_RETENTION_DAYS` env (default `4`, set to `0` to disable). Once QNAP is up with its own retention, the local prune is harmless overlap.
- [x] **Clear `gpu:generation_lock` on startup** — `_clear_comfyui_queue_on_startup()` now deletes both `gpu:generation_active` and `gpu:generation_lock`. Without this, an unclean shutdown mid-image-gen blocked the next gen for up to 6 min (lock TTL).
- [x] **Clear `identity_state` when scene empties** — `face-recognizer/recognizer.py` now `r.delete(IDENTITY_KEY)` when an incoming pose-detection message has zero detections. Stops the dashboard from showing stale face labels after the room empties. Sticky behavior when faces are merely turned away is preserved (only triggers on no-people).
- [x] **`target_fps` hot-reload in ingester** — `ingester.py` now polls `config:{camera_id}` every 25 frames and rebuilds `frame_interval`. The dashboard FPS slider is now actually wired through end-to-end. Was previously a dead control.

### Tier 2 — High-impact functional bugs

- [ ] **Sticky-identity cache leak** (`services/dashboard/server.py:~898`). The cache is stored as a function attribute on `websocket_live`, shared across all concurrent WebSocket clients. Two browser tabs corrupt each other's labels. Empty-string `person_id`s also trade in/out of the cache every frame. Move into a per-connection dict, or better, into Redis with a TTL.
- [x] **Telegram polling offset not persisted** — Fixed in `routes/bot_commands.py`. Now reads `telegram:last_offset` from Redis at poller startup and `SET`s after every processed update. Verified by setting key, restarting, observing `Telegram offset restored from Redis: <value>` log.
- [ ] **Schema drift in pose detector** (`services/pose-detector/detector.py:~315`). Reads `data[b"frame"]` only — will KeyError if a frame ever lands without that field. Vehicle and tracker already do `data.get(b"frame") or data.get(b"frame_bytes")`. Match the defensive pattern for symmetry.
- [ ] **Vehicle stationarity reference center never resets** (`tracker.py:215-228`). `is_stationary` measures displacement from `center_history[0]` which is set on first detection and never updated. A parked car briefly nudged 31 px is "non-stationary forever." Either rolling-window the reference, or reset after `vehicle_idle` fires once.
- [ ] **GPU pause race condition** (`routes/image_gen.py:855` vs detectors). Dashboard unloads Ollama models *before* setting the GPU pause flag — detectors can have inference mid-flight when ComfyUI loads weights, causing OOM or 11+ min cold loads. Add a heartbeat: detectors set `detector:{name}:paused` Redis key when they yield; image_gen waits for those before unlocking GPU.
- [x] **face-recognizer doesn't honor `gpu:generation_active`** — Fixed in `recognizer.py`. Added the same pause check pattern pose-detector + vehicle-detector use, logging `GPU generation active — pausing face recognition...` and `resuming` once per transition. Verified: `SET gpu:generation_active 1 EX 5` triggered pause+resume logs cleanly.
- [ ] **Dead-zone normalized-coords mismatch** when HD frame is shown (`server.py:~95`). Decide once whether dead-zone test runs against sub-stream coords, then enforce that both code paths agree.

### Tier 3 — Hardcoded values that should be config

- [ ] **Grafana admin password** (`docker-compose.yml:347`) → `GRAFANA_ADMIN_PASSWORD` env var.
- [ ] **Ollama model strings** hardcoded in 3 places: `routes/ai.py:53`, `routes/bot_commands.py:1193`, `server.py:395` (warmup). Pull into a single constant or env var (`OLLAMA_CHAT_MODEL`), error clearly if model isn't pulled.
- [ ] **MiniCPM-V model** is at least uniformly read via `os.getenv("VISION_MODEL", "minicpm-v")` — but `keep_alive="5m"` is hardcoded in 5+ call sites. Pull into config too.
- [ ] **Default ComfyUI checkpoint** `"zillah.safetensors"` hardcoded in `routes/image_gen.py:135`. Will fail silently on systems without that file. Either error explicitly when no checkpoint found, or default to first available `.safetensors` in `models/comfyui/checkpoints/`.
- [ ] **`MAX_UNKNOWN_FACES = 100`** and **`UNKNOWN_DEDUP_THRESHOLD = 0.6`** in `face_db.py:34,38` → env vars.
- [ ] **camera-ingester `REDIS_HOST=127.0.0.1`** (works only because of host net) — leave as-is but document why in a comment.

### Tier 4 — Auth & security hardening

- [x] **`/ws/live` WebSocket auth bypass** — Fixed. `websocket_live` now reads `vl_session` cookie, calls `validate_session()`, and closes with code `4401 Unauthorized` if invalid. Verified: cookie-less WS connection rejected, valid-cookie WS receives frames.
- [x] **face-recognizer port 8081 exposed unauthenticated** — Fixed in `docker-compose.yml`. Replaced `ports: ["8081:8081"]` with `expose: ["8081"]` (Docker DNS only). Verified: `curl http://localhost:8081/api/faces` connection refused; dashboard `/api/faces` proxy still works.
- [x] **Telegram bot token leaks into log lines** — Fixed in `server.py`. Set `logging.getLogger("httpx").setLevel(logging.WARNING)` so httpx no longer prints request URLs. Verified: no `bot85...` strings in dashboard logs after restart.
- [x] **Default `admin/admin`** — Fixed. Login endpoint detects when admin still has default password and returns `must_change_password: true`. `login.html` swaps to a forced-rotation form; user must set a new password (≥8 chars, ≠ "admin") before reaching the dashboard. Session cookie is still issued so the change-password call works, but the UI gates entry. Verified: response shows the flag.
- [ ] **SHA-256 salted hashing** in `routes/auth.py:130` — switch to bcrypt or argon2id. Migrate existing hashes lazily on next successful login.
- [ ] **Auth secret stored in same SQLite as the data it protects** (`/data/auth.db` `app_config` table). If the DB leaks, all session cookies are forgeable indefinitely with no rotation. Move to env var or rotate on a schedule.
- [ ] **Telegram bot token possibly logged** in error responses (`notifications.py:171,213` log `resp.text` from Telegram API). Strip or redact tokens from response bodies before logging.
- [ ] **Per-event-type rate limits are global**, not per-user (`notifications.py:75,773`). One person's "loud day" mutes everyone's alerts. Per-user cooldown when broadcasting to multiple Telegram users.

### Tier 5 — Robustness

- [ ] **Graceful shutdown** for background pollers + metrics collector in `server.py`. Currently `while True` loops with no shutdown flag — asyncio cancels them mid-iteration leaving partial files / half-written snapshots.
- [ ] **`recorder.py:217` blocking `stderr.read()`** after ffmpeg exits — if ffmpeg is OOM-killed and never wrote stderr, this blocks indefinitely. Add a timeout.
- [ ] **Recorder doesn't notify on segment failure** — ffmpeg can lose RTSP for hours and the dashboard has no idea recording is broken. Emit `recorder_error`/`recorder_recovered` events to `events:{cam}`.
- [ ] **`_event_notification_poller` starts at `last_id="$"`** — events emitted before dashboard restart are skipped (snapshots saved, but Telegram alerts lost). For a HA deployment, persist `last_id` per consumer.
- [ ] **Snapshot file collision on tracker restart** — `person_snapshot:{cam}:{int(timestamp)}` Redis key uses second-precision. If two events land in the same second across tracker restart, second overwrites first. Use full float timestamp or stream message ID.
- [ ] **Tracker `count=1` xreadgroup limit** (`tracker.py:909`) — only one detection per loop iteration. Fine at 5 FPS, will fall behind at higher rates. Bump to `count=10` or so.
- [ ] **`_gen_params` dict in `image_gen.py:42`** can leak entries on certain success paths. Eviction-from-success exists but only on cancel path; harden.
- [ ] **Recorder `.ts` vs Dockerfile comment claiming `.mp4`** — fix one or the other. (Either remux at segment-close or update the comment.)
- [x] ~~**Empty Grafana dashboards directory**~~ — Resolved. `services/grafana/dashboards/vision-labs.json` is a real, provisioned dashboard with multiple panels (verified in audit). The PHASES.md prediction about it being empty was stale.
- [ ] **Inconsistent logging defaults** — `face_db.py` doesn't call `logging.basicConfig`, so its messages route to root's default handler while other services explicitly configure. Standardize.

### Tier 6 — Stale documentation

- [ ] `contracts/streams.py:11-14` describes "rule engine (Phase 4)" as if a separate service. The dashboard event poller absorbed that role; update docstring.
- [ ] `tracker.py:31` mentions "Phase 5 adds face-based re-identification" — face recognition is a separate service now; description is partly stale.
- [ ] `state:{cam}.persons` field is read by `server.py:891` and `ai_tools.py:421` as a fallback, but the tracker only ever writes `state:{cam}.people`. Either rename writes or remove the dead branch.
- [ ] All services default `CAMERA_ID=front_door`. Multi-camera support requires aligning 7 services in lockstep — document this constraint or build a proper multi-camera config.

**Exit criterion:** none individually blocks anything — just chip away. Re-run Phase 4 smoke tests after each change.

---

## Phase 6 — New features (TBD)

Hold this for after Phase 5. List ideas here as they come up so we don't lose them. Examples that fit the existing architecture cleanly:

- [ ] _(your idea here)_

For each new feature, default pattern is: extend `contracts/streams.py` with new stream/key, add a service or dashboard route, no direct service-to-service HTTP unless justified.

---

## Open questions to resolve as we go

- [ ] Is the QNAP alive? (Phase 1)
- [ ] What subnet is the camera actually on? (Phase 2)
- [ ] What new env vars do you want to add beyond the existing list? Track them at the top of Phase 1.
- [ ] Do you want to keep dual streams (sub + HD) on WiFi, or simplify to sub-only to ease bandwidth?
