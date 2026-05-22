# Changelog

All notable changes to Vision Labs. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) · Versions: [SemVer](https://semver.org/).

Release images publish to `ghcr.io/gammahazard/vision-labs/<service>:<tag>` (`:vX.Y.Z` + `:latest`).

---

## [Unreleased]

### Added
- **Edit pencil ✏ on camera rows** — modal to rename, edit lat/lon, toggle detectors without delete + re-add.
- **Vehicle attributes Phase 1** — per-cam `vehicle-attributes-cam{N}` service buffers HD crops, writes per-track dirs (`hero.jpg` + `angle_NN.jpg` + `metadata.json`) on track end. New `detect_vehicle_attributes` flag, `vehicle_sample` tracker event, Browse grouped cards. No classifier yet. *Requires new service build + tracker rebuild.*
- **Vehicle attributes Phase 3 (v0 classifier)** — ConvNeXt-Tiny multi-head fills `metadata.json.attributes` with color + body + make (always) + model (drive-by tracks only). Gated by `ENABLE_CLASSIFIER` env (default 0); deploys before trained weights exist. *Requires vehicle-attributes rebuild + HF Hub-hosted weights when enabled.*

### Fixed
- **Detector-flag dependencies enforced** — `detect_faces` requires `detect_persons` now hard-gated UI (`data-requires=`) + server (`_validate_camera`).
- **Mid-run `detect_*` toggles take effect** — `upsert_camera` publishes pre-expanded `{prefix}-{profile}` on `config:apply`; orchestrator routes to the single affected camera. *Requires orchestrator rebuild.*
- **`vehicle_left` spammed events panel for drive-bys** — split producer: new internal `vehicle_gone` for all track ends; `vehicle_left` now idle-leave only (gated on `idle_alerted`). *Requires tracker + vehicle-attributes rebuild.*
- **IoU identity-swap on fast-moving vehicles** — added `_try_live_center_match` fallback after the IoU step. *Requires tracker rebuild.*
- **`vehicle_sample` + `vehicle_gone` leaked into events panel** — filtered server-side in `routes/events.py`; stream still carries them for the attribute service. *Dashboard restart only.*
- **IoU center-distance ratio too tight for wide-angle cams** — bumped `VEHICLE_GHOST_MAX_DIST_RATIO` 2.0 → 3.5 (catches 225-px shifts seen on cam1 fish-eye). *Requires tracker rebuild.*
- **Vehicle-attributes per-track dirs invisible to Browse** — Phase 1 compose mounted `snapshot-data` but dashboard reads `qnap-snapshots`; the data was being written and read on two different volumes. All 20 vehicle-attributes-camN blocks now mount `qnap-snapshots`; orphan `snapshot-data:` volume removed. *Requires per-cam vehicle-attributes recreate.*
- **Vehicle-attribute crops misaligned with bbox** — generic `frame_hd:{cam}` fetch by vehicle-attributes drifted relative to the bbox's detection moment, so crops landed on background. Now vehicle-detector ships `hd_frame_bytes` paired with each detection; tracker writes a per-sample `vehicle_hd_sample:{cam}:{vid}:{ms}` key (60 s TTL); vehicle-attributes reads from that key (falls back to generic `frame_hd` if missing). Mirror of v0.2.0 person-snapshot drift fix. *Requires vehicle-detector + tracker + vehicle-attributes rebuild.*

### Changed
- **Browse day view simplified** — dropped the confusing "Per-track view (N tracks)" section. Day view is now the legacy flat snapshot grid + a single `📸 Vehicle crops taken (N)` button at the top. Click → modal with grouped-by-track thumbnails. Click a thumbnail → existing fullscreen photo viewer. `/api/browse/days` now also returns `track_count` per day.
- **Classifier split into two ConvNeXt-Tiny models** — frozen ImageNet backbone + linear color head (VeRi-776 trained) and fine-tuned Stanford-Cars backbone + body/make/model heads; one shared backbone couldn't serve both training regimes. Adds IR/night-vision skip-color path (`IR_SATURATION_THRESHOLD`, default 8.0) so the color head doesn't emit noise on monochrome night frames. *Requires vehicle-attributes rebuild + new `color_head_v0` / `multihead_v0` weights on HF Hub.*

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
