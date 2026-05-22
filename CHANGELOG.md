# Changelog

All notable changes to Vision Labs. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) · Versions: [SemVer](https://semver.org/).

Release images publish to `ghcr.io/gammahazard/vision-labs/<service>:<tag>` (`:vX.Y.Z` + `:latest`).

---

## [Unreleased]

### Added
- **Edit pencil ✏ on camera rows** — modal to rename, edit lat/lon, toggle detectors without delete + re-add.
- **Vehicle attributes Phase 1** — per-cam `vehicle-attributes-cam{N}` flushes per-track HD crops + `metadata.json` on track end. *Requires new service build + tracker rebuild.*
- **Vehicle attributes Phase 3 v0 classifier** — fills `metadata.json.attributes` with color/body/make/model, gated by `ENABLE_CLASSIFIER`. *Requires vehicle-attributes rebuild + HF weights.*

### Fixed
- **Brief drive-bys produced 0 or 1 buffered crops** — new-vehicle branch never emitted `vehicle_sample`, and the every-3rd throttle skipped 2/3 of frames. A red pickup at 12:09 with 6 detector hits across two tracks produced 1 stored crop. Spawn frame now always emits a sample; first 8 matched frames per track also emit unconditionally (`EAGER_SAMPLE_FRAMES`, matches `buffer.max_crops`); throttle kicks in only after the reservoir would be full. *Requires tracker rebuild.*
- **Parked-car tracks absorbed drive-by crops in their first 150 s** — tight-IoU gate now triggers on `is_stationary or idle_alerted` (was idle_alerted-only, fires at +150 s). *Requires tracker rebuild.*
- **YOLO car↔truck↔bus flicker was splitting one vehicle into multiple tracks** — fallback IoU paths required strict class equality; YOLOv8 flips a single physical vehicle between car/truck/bus across frames (pickup at 11:35 became vehicle_0009 'car' + vehicle_0010 'truck' 2 s later). 4-wheel classes are now interchangeable in `_try_ghost_match` + `_try_live_center_match`; bicycle/motorcycle stay strict. Same-frame guard prevents two distinct detections in one batch from merging. *Requires tracker rebuild.*
- **Per-track dir names now include first-seen timestamp** — `vehicle_0001_<epoch>` instead of just `vehicle_0001`, so a new tracker session that re-mints `vehicle_0001` can't silently overwrite a previous physical vehicle's `hero.jpg` + `metadata.json` on the same day. *Requires vehicle-attributes rebuild.*
- **Classifier confidence now visible when a head votes below threshold** — `color_confidence` + `model_confidence` were nulled out when their label fell under the cutoff, hiding 'was 0.53, just under 0.55' vs 'was 0.18, way off'. Now `conf=None` strictly means 'head not run' (IR-suppressed color); below-threshold votes report the actual conf. *Dashboard restart only.*
- **Model head now runs on idle tracks too** — original spec deferred it to drive-by-only out of caution, but parked cars give well-sampled multi-angle views that should be easier to classify, not harder. Worth seeing weak predictions and tuning the threshold from data. *Requires vehicle-attributes rebuild.*
- **Drive-by polluted an idle parked car's crops at IoU ~0.3** — `VEHICLE_IDLE_IOU_THRESHOLD` (default 0.65) for idle tracks; loose center-distance fallbacks skip idle tracks. *Requires tracker rebuild.*
- **Events panel returned 0 for cam1** — internal `vehicle_sample` writes saturated the tail; reader now overscans (`MAX_REDIS_SCAN`, default 2000) until N user events surface. *Dashboard restart only.*
- **`ModuleNotFoundError: torch` in vehicle-attributes** — Dockerfile `pip install` landed in python3.10; service runs python3.11. Switched to `python -m pip install`. *Requires vehicle-attributes rebuild.*
- **Same parked car spawning a fresh track every detector hiccup** — added `VEHICLE_IDLE_GHOST_TTL` (default 600 s); idle-confirmed tracks keep their ghost slot through long gaps. *Requires tracker rebuild.*
- **Classifier null on every flush despite torch loading** — class JSONs in container were PR #22 stubs (body=8, make=50); trained weights expect body=9, make=49. Copied real JSONs from `training-output/`. *Requires vehicle-attributes rebuild.*
- **Detector-flag dependencies enforced** — `detect_faces` now hard-gated on `detect_persons` (UI + server).
- **Mid-run `detect_*` toggles take effect** — `upsert_camera` ships pre-expanded `{prefix}-{profile}` on `config:apply`. *Requires orchestrator rebuild.*
- **`vehicle_left` spammed events panel for drive-bys** — new internal `vehicle_gone`; `vehicle_left` now idle-leave only. *Requires tracker + vehicle-attributes rebuild.*
- **IoU identity-swap on fast-moving vehicles** — added `_try_live_center_match` fallback after the IoU step. *Requires tracker rebuild.*
- **`vehicle_sample` + `vehicle_gone` leaked into events panel** — filtered server-side in `routes/events.py`. *Dashboard restart only.*
- **IoU center-distance ratio too tight for wide-angle cams** — bumped `VEHICLE_GHOST_MAX_DIST_RATIO` 2.0 → 3.5. *Requires tracker rebuild.*
- **Vehicle-attributes per-track dirs invisible to Browse** — 20 compose blocks switched from `snapshot-data` → `qnap-snapshots` to match the dashboard. *Requires per-cam vehicle-attributes recreate.*
- **Vehicle-attribute crops misaligned with bbox** — vehicle-detector ships HD bytes inline with detection; tracker writes per-sample `vehicle_hd_sample:*`. *Requires 3 service rebuilds.*
- **`person_identified` events never fired despite cyan bbox** — replaced delete-on-empty with TTL refresh (`IDENTITY_KEY_TTL_SEC=5`). *Requires face-recognizer rebuild.*
- **Brief drive-bys never produced a per-track dir** — `main.py` gated the ghost sweep on non-empty detections; single-frame cars sat in `tracked_vehicles` forever. Now sweeps every poll. *Requires tracker rebuild.*

### Changed
- **Browse day view simplified** — flat snapshot grid + single `📸 Vehicle crops taken (N)` button opening a per-track modal.
- **Classifier split into two ConvNeXt-Tiny models** — frozen-backbone color + Cars-fine-tuned body/make/model; adds IR-frame skip-color path. *Requires vehicle-attributes rebuild.*

---

## [0.2.0] — 2026-05-21

### Added
- `js/lib/safe-html.js` — single canonical `_PURIFY_CFG` + `_safeHtml()`. Fixes four-way duplicate-declaration `SyntaxError`.
- `js/lib/dompurify.min.js` (vendored, ~22 KB). Closes 12 CodeQL XSS/incomplete-sanitization findings.
- `CHANGELOG.md` (this file).
- `install-linux.sh` defaults to GHCR pull; `--build` flag for forkers.
- Orchestrator threads `EXTRA_COMPOSE_FILES` through every compose call so registry-pull installs keep pulling when adding cam2–cam20.
- README: `--build` + `IMAGE_TAG=vX.Y` pinning; native Mermaid architecture diagram; centered "Live metrics" Grafana GIF.
- `tests/test_bot_commands_no_nameerror.py` — smoke check over every bot command + dispatcher; asserts no `is not defined` / `has no attribute` / `cannot import name` in outbound messages.
- `/audit-repo` skill (`.claude/skills/audit-repo/`) — 4-track fan-out (docs drift, code quality, architecture, schema-drift) with file:line evidence invariant. First run found 5 latent NameError bugs + 1 path traversal + 2 schema-drift criticals. ~30–40 min wall-clock, 100+ subagent dispatches per run.
- `.claude/settings.json` — project-shared permission allowlist.
- `.github/dependabot.yml` — weekly grouped patch/minor PRs per service. Ignores semver-major.
- `tests/test_orchestrator.py` — 53 tests (was 0). Covers cred scrub, profile allowlist, `compose_down` sequencing, config-apply allowlist, `desired_profiles` Redis-failure sentinel, audit schema, nvidia-smi parser, reconcile diff, `_run_compose` edges.
- **Ruff lint gate in CI** (`tests.yml` job `lint`, Pyflakes F-rules only, pinned `ruff==0.15.13`). F821 (undefined-name) catches the NameError-class regressions in CLAUDE.md §0 at PR time. See CLAUDE.md §7.

### Fixed
- **Grafana ran in UTC** regardless of `LOCATION_TIMEZONE` — wired `TZ` + `GF_DATE_FORMATS_DEFAULT_TIMEZONE`. `grafana` added to `CONFIG_APPLY_ALLOWED_SERVICES`.
- **Retention settings did nothing end-to-end** — 20 `recorder-cam{N}` services hardcoded `RETENTION_DAYS=3`; dashboard had no retention env wired at all (DVR tab showed "28 days" no matter what). All three retention vars now `${VAR:-default}`-interpolated; dashboard env block populated.
- **Settings panel didn't restart services after Save** — `config:apply` sent bare `recorder`, compose failed atomically with `no such service: recorder`, dashboard never restarted either. New `_expand_per_cam_services()` expands bare names against registry-enabled profiles + prepends `--profile camN` flags. `tracker` added to allowlist. *Requires orchestrator rebuild.*
- **Browse tab stuck on "Loading snapshots…"** — duplicate top-level `const _PURIFY_CFG` threw `SyntaxError` killing the second script; then DOMPurify stripped inline `onclick` handlers. Extracted to `safe-html.js`; refactored `browse.js` to event delegation via `data-action=`.
- **`person_appeared` snapshots showed bbox on empty floor** — `xrevrange(FRAME_STREAM, count=1)` returned a frame N frames ahead of the detection. Pose-detector now ships `frame_bytes` on detection messages; tracker buffers `last_frame_bytes` per `TrackedPerson`. *Requires pose-detector + tracker rebuild.*
- **face-recognizer face crops landed N frames after the bbox** — same `xrevrange` bug class. Now reads `frame_bytes` directly from the detection-stream message. *Requires face-recognizer rebuild.*
- **`find_dvr_segment` ignored `RECORDING_DIR`** — hardcoded `/data/recordings/...`. Now reads env at module load.
- **404s on `/api/events/{id}/snapshot` for `person_left`** — `event_renderer.py` set `photo.kind="event_snapshot"` for events that never had snapshots saved.
- **5 NameError bugs surfaced by `/audit-repo`** (same family as v0.1.1 bot_commands regression):
  - `routes/telegram_access.py:63`, `pollers/events.py:105,261` — bare `TZ_LOCAL`, now imported from `contracts.tz`.
  - `routes/ai_tools/send_telegram.py` — `_send_telegram_rate_check` + `_SEND_TG_*` undefined → implemented as sliding-window limiter (10/60 s).
  - `routes/ai_tools/schedule_reminder.py` — `_parse_time` + `_MAX_PENDING_REMINDERS` undefined → implemented (ISO 8601 + relative + time-of-day; cap 50).
- **`test_send_telegram` + `test_schedule_reminder`** were early-returning before the regression code paths. Both now exercise full happy-path.
- **Schema-drift `hash:state:{cam}`** — `bot_commands/who.py` read `num_vehicles` + `vehicles` that the tracker never writes. Removed dead block.
- **Schema-drift `hash:config:{cam}`** — `min_keypoints`, `kp_confidence_thresh` missing from `DEFAULT_CONFIG`. Added; startup `HSETNX` backfill propagates missing keys to existing per-camera config hashes (non-destructive).
- **Tracker-pipeline audit findings (5):**
  - vehicle-detector now emits `frame_width`/`frame_height`; tracker reads on vehicle stream. *Requires vehicle-detector + tracker rebuild.*
  - vehicle snapshot key second→millisecond resolution prevents same-second collisions. *Requires tracker rebuild.*
  - `faces.db` SQLite WAL mode + 10 s connect timeout (latent concurrent-reader lock). *Requires face-recognizer rebuild.*
  - `IDENTITY_KEY` Redis hash gets 5-min TTL (stale identity overlay if recognizer crashed). *Requires face-recognizer rebuild.*
  - Recorder retention sweep refuses to follow symlinks. *Requires recorder rebuild.*
- **`vehicle_idle` Telegrams now respect per-zone time-of-day rules** — notify path skips send when zone configured AND `alert_triggered==False`.
- **Per-vehicle dedup on `vehicle_idle`** — SETNX `notify:vehicle_idle:seen:{cam}:{vehicle_id}:{first_seen}` 1 h TTL. *Requires tracker rebuild + dashboard restart.*
- Pose + vehicle detectors used `time.time()` for inference duration → NTP corrections caused negative `inference_ms` spikes in Grafana. Switched to `time.monotonic()`.
- `routes/notifications/frame.py:build_clip` opened fresh Redis connection per call; now reuses `ctx.r_bin`.
- WebSocket Redis-connection leak — `make_redis_client(decode_responses=False)` per session with no cleanup. Wrapped in `try/finally: r_bin.close()`.
- Stripped exception details from 16 HTTP error responses (`py/stack-trace-exposure`). Now `logger.exception(...)` server-side + generic message to client.
- `CLAUDE.md` §7 + §12 claimed `258 tests`; actual is `302`.
- `CONTEXT.md` §12 session-token format documented pre-`must_change_flag` 3-part shape; updated to 4-part.

### Changed
- Home conditions panel matches single-cam view (Wind, Visibility, time-of-day schedule). Fixes `Cannot set properties of null` at `conditions.js:164`.
- Home dashboard loads with all six panels collapsed.
- Tracker stamps `vehicle_id` + `vehicle_first_seen` on every vehicle event. *Requires tracker rebuild + dashboard restart.*
- Dependency bumps (Dependabot, patch/minor only):
  - dashboard: `fastapi` 0.115 → 0.136.1, `uvicorn` 0.32 → 0.47, `redis` 5.2.1 → 5.3.1, `opencv-python-headless` 4.10 → 4.13, `httpx` 0.27 → 0.28.1, `ollama` 0.4 → 0.6.2, `prometheus_client` 0.20 → 0.25, `bcrypt` 4.0 → 4.3, `Pillow` 10 → 10.4, `numpy` 1.24 → 1.26.4, `python-multipart` 0.0.6 → 0.0.29
  - tracker: `redis` 5.2.1 → 5.3.1, `numpy` 1.24 → 1.26.4
  - recorder, camera-ingester: `redis` 5.2.1 → 5.3.1; camera-ingester also `opencv-python-headless` 4.10 → 4.13
  - GH Actions: `actions/checkout` v4 → v5, `actions/setup-python` v5 → v6
  - *Requires rebuilding affected service images.*

### Security
- **Path traversal in `routes/bot_commands/ask.py:195`** — LLM-controlled `date_part` interpolated into `os.path.join`. Now regex-gated `^\d{4}-\d{2}-\d{2}$`.
- **Path-injection in `routes/events.py`** — `resolve_event_snapshot_path` interpolated raw `camera_id`. Replaced with canonical `os.path.realpath(...).startswith(realpath(BASE) + os.sep)` containment + re-check at `open()` sink. Closes 4 `py/path-injection` alerts.

### Removed
- Stale `from fastapi.staticfiles import StaticFiles` in `server.py` (replaced by aliased `_StaticFiles`).
- Stale `routes/clips.py` reference in `CONTEXT.md` (file already deleted).
- `.github/workflows/claude-code-review.yml` — auto-review-on-PR was costing ~$4.29/run. Opt-in `@claude` workflow preserved.

---

## [0.1.1] — 2026-05-20

### Added
- `ollama_warmup.py` auto-pulls `VISION_MODEL` alongside `CHAT_MODEL` on first boot.
- `/zones` Telegram command shows inline camera picker when multiple cameras configured.

### Fixed
- Six Telegram commands raised `NameError` due to imports lost during the R3 split of `bot_commands.py`:
  - `/events`, `/status` — `make_redis_client`, `REDIS_HOST`, `REDIS_PORT`
  - `/analyze`, `/ask` — `OLLAMA_*`
  - `/timelapse` — `SNAPSHOT_DIR`
  - `/clip` — cross-module helpers `_extract_clip_frames`, `_describe_scene_multi`
- Constants now surfaced through `_shared.py`.

### Changed
- Setup walkthrough GIF: 800 px × 10 fps (was 560 px × 8 fps). 3.4 MB → 6.8 MB; centered in README.
- `DETAILED_README` install section leads with registry-pull; local build is secondary.

---

## [0.1.0] — 2026-05-20

First tagged release. Initial publish of 9 service images to GHCR.

**Stack**
- Multi-camera (1–20 slots) AI security platform on Docker Compose + Redis Streams.
- YOLOv8s-pose (persons), YOLOv8s (vehicles), InsightFace `buffalo_l` (faces).
- Qwen 3 14B chat assistant (19 tools); MiniCPM-V for Telegram scene descriptions.
- FastAPI dashboard with WebSocket live grid, DVR playback, face enrollment, drawable zones, Telegram pairing.
- ONVIF unicast WS-Discovery for setup (works in WSL2 mirrored networking).
- Prometheus + Grafana embedded; Portainer for container management.
- 17-command Telegram bot with multi-camera awareness + admin role gating.
- DVR retention defaults: 28 d recordings / 4 d snapshots / 3 d clips.

**Requirements**
- NVIDIA driver R555+ (CUDA 12.8) — required for Blackwell (RTX 50-series).
- Docker Engine + `nvidia-container-toolkit`.
- Linux (Ubuntu 22.04/24.04, Debian 12) or Windows 11 + WSL2.

**Tested**
- Single host: RTX 5070 Ti (16 GB) + RTX 3090 (24 GB), Ubuntu 24.04 inside WSL2 mirrored networking.
- Single-GPU works; defaults assume 8–12 GB.

**Known limitations**
- macOS not supported (CUDA-only inference path).
- Single-user auth (one admin, bcrypt + HMAC sessions); no team/role model.
- LAN-only by design; reverse proxy + `DASHBOARD_BEHIND_TLS=true` if exposing.
- Qwen 3 14B reliable on focused questions; compound multi-part can be muddled.

---

[Unreleased]: https://github.com/gammahazard/vision-labs-v2/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/gammahazard/vision-labs-v2/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/gammahazard/vision-labs-v2/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/gammahazard/vision-labs-v2/releases/tag/v0.1.0
