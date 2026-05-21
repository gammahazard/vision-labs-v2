# Changelog

All notable changes to Vision Labs are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow [SemVer](https://semver.org/).

Images for each tagged release are published to GitHub Container Registry at `ghcr.io/gammahazard/vision-labs/<service>:<tag>` (both the literal tag and `:latest`).

## [Unreleased]

### Added
- `CHANGELOG.md` — this file.
- `install-linux.sh` defaults to pulling from GHCR; `--build` flag for forkers.
- Orchestrator threads `EXTRA_COMPOSE_FILES` through every `docker compose` call so registry-pull installs keep pulling when adding cam2–cam20 from the dashboard.
- README Quick install section now mentions `--build` + `IMAGE_TAG=vX.Y` pinning.
- README architecture diagram converted to native GitHub Mermaid; added centered "Live metrics" section with a Grafana GIF.
- `tests/test_bot_commands_no_nameerror.py` — exhaustive smoke check covering every Telegram bot command handler + the dispatcher. Mirrors `test_ai_tools_no_nameerror.py`. Captures outbound Telegram messages and fails on regression-class signatures (`is not defined`, `has no attribute`, `cannot import name`), catching both bare exceptions AND try/except-wrapped NameErrors — the exact failure mode that hid the v0.1.1 bot_commands regression in production. Verified by temporarily dropping `make_redis_client` from `events.py` imports — test fails with the literal production error string.

### Fixed
- Pose + vehicle detectors used wall clock (`time.time()`) for inference duration, so NTP corrections on WSL2 host-resume could produce negative `inference_ms` values that pulled the Grafana "YOLO Inference Time" mean below zero (visible as -2s spikes / -7.25s means on hour-zoom views). Switched both detectors to `time.monotonic()`. Requires rebuilding the affected detector images.
- `routes/notifications/frame.py` `build_clip()` opened a fresh Redis connection on every call instead of reusing the shared `ctx.r_bin`. Each Telegram clip + AI `capture_clip` request leaked a TCP connection; now uses the shared client.
- **Five latent NameError bugs surfaced by the /audit-repo skill's first run (all same family as the v0.1.1 bot_commands regression in CLAUDE.md §0):**
  - `routes/telegram_access.py:63` (approve_user) and `pollers/events.py:105, 261` (`_journal_event`, vehicle-snapshot save) referenced bare `TZ_LOCAL` but only imported `ZoneInfo`. Both files now `from contracts.tz import TZ_LOCAL` (canonical SSOT at `contracts/tz.py:66`). Every `POST /api/telegram/users` call and every past-date journal write / vehicle snapshot was NameError-ing into a swallowed try/except.
  - `routes/ai_tools/send_telegram.py` referenced `_send_telegram_rate_check()`, `_SEND_TG_MAX_PER_WINDOW`, `_SEND_TG_WINDOW_SEC` — defined nowhere. Implemented as a sliding-window rate limiter (10 sends per 60 s, in-process `collections.deque` + `threading.Lock`) so the AI tool can't spam Telegram.
  - `routes/ai_tools/schedule_reminder.py` referenced `_parse_time()` and `_MAX_PENDING_REMINDERS` — defined nowhere. Implemented `_parse_time` covering ISO 8601 (`2026-02-21T22:00:00`), relative offsets (`in 5 minutes`, English number words), and time-of-day (`10:00 PM` rolling to tomorrow if past). `_MAX_PENDING_REMINDERS = 50`.
- **Closed two `tests/test_ai_tools_no_nameerror.py` early-return gaps the audit also exposed** — `test_send_telegram` was returning at the `is_configured()` check before reaching the rate-limit code; `test_schedule_reminder` was using wrong arg keys (`{text, when}` instead of `{message, time_description}`) causing args-validation early-return. Both now exercise the full happy-path. Without these fixes, the existing test suite would have continued silently passing while production NameError'd.
- **Schema-drift in `hash:state:{cam}`:** `routes/bot_commands/who.py` read `num_vehicles` + `vehicles` from the state hash, but the tracker writes neither — the /who vehicle block was permanently dead. Removed the dead block. /who now reports person info only. Adding vehicle support back is a follow-up that needs tracker code changes (and an image rebuild).
- **Schema-drift in `hash:config:{cam}`:** Pose-detector reads `min_keypoints` + `kp_confidence_thresh` from per-camera config, but `DEFAULT_CONFIG` in `routes/__init__.py` never seeded them — cameras silently fell back to pose-detector env defaults instead of the documented config-UI flow. Added both fields to `DEFAULT_CONFIG` (defaults: `"3"` and `"0.3"`, matching pose-detector). Also added a startup backfill loop in `server.py` that walks `cameras:registry` and `HSETNX`s every `DEFAULT_CONFIG` key onto each existing per-camera config — non-destructive (only sets if absent, so user-customized values are preserved), and propagates this *and* any future `DEFAULT_CONFIG` schema additions to already-registered cameras automatically. Verified on a 2-camera install: `Backfilled 13 missing DEFAULT_CONFIG key(s) across 2 camera config hash(es)`.
- `CLAUDE.md` §7 and §12 claimed `258 tests`; actual count is `302`. Updated.

### Security
- **Path traversal in `routes/bot_commands/ask.py`** (vehicle-snapshot URL handler, line 195). The `/ask` chat path renders LLM tool-output URLs like `/api/browse/snapshot/{date}/{filename}` by `os.path.join(snap_dir, date_part, safe_name)`. `safe_name` was already sanitized via `os.path.basename()`, but `date_part` (LLM-controlled) was passed raw — a crafted URL like `/api/browse/snapshot/../../etc/some.jpg` would let an LLM hallucination (or prompt injection) read arbitrary files from the dashboard container. Now requires `date_part` to match `^\d{4}-\d{2}-\d{2}$`; mismatches are silently skipped.

### Removed
- Stale `from fastapi.staticfiles import StaticFiles` import in `services/dashboard/server.py` (the actual mount uses an aliased `_StaticFiles` import further down).
- Stale CONTEXT.md reference to `routes/clips.py` as orphaned — file was already deleted; doc was lagging.

## [0.1.1] — 2026-05-20

### Added
- `ollama_warmup.py` now auto-pulls `VISION_MODEL` (MiniCPM-V) alongside `CHAT_MODEL` on first boot. Closes a first-run gap where Telegram alerts and `/analyze` would fail with "model not found" until manually pulled.
- `/zones` Telegram command now shows an inline camera picker when more than one camera is configured (matching `/snapshot` and `/clip`).

### Fixed
- Six Telegram commands raised `NameError` at invocation due to imports lost during the Phase R3 modularization of `bot_commands.py`:
  - `/events`, `/status` — `make_redis_client`, `REDIS_HOST`, `REDIS_PORT`
  - `/analyze` — `OLLAMA_KEEP_ALIVE`
  - `/ask` — `OLLAMA_HOST`, `OLLAMA_MODEL`, `OLLAMA_KEEP_ALIVE`
  - `/timelapse` — `SNAPSHOT_DIR`
  - `/clip` — cross-module helpers `_extract_clip_frames`, `_describe_scene_multi`
- Constants now surfaced through `_shared.py` so future commands can import from a single place.

### Changed
- Setup walkthrough GIF re-encoded at 800px × 10 fps (was 560px × 8 fps). 3.4 MB → 6.8 MB; still under GitHub's 10 MB README limit, sharper on desktop.
- Setup GIF now centered in README via inline HTML wrapper.
- DETAILED_README install section flipped to lead with the registry-pull path; local build is now the secondary option.

## [0.1.0] — 2026-05-20

First tagged release. Triggers initial publish of 9 service images to GHCR.

### Stack
- Multi-camera (1–20 slots) AI security platform on Docker Compose + Redis Streams.
- YOLOv8s-pose person + pose detection, YOLOv8s vehicle detection, InsightFace `buffalo_l` face recognition.
- Qwen 3 14B chat assistant with 19 tool functions; MiniCPM-V vision LLM for Telegram scene descriptions.
- FastAPI dashboard with WebSocket live grid, per-camera detail view, DVR playback, face enrollment wizard, drawable zones, Telegram pairing.
- ONVIF unicast WS-Discovery for camera setup (works in WSL2 mirrored networking).
- Prometheus + Grafana monitoring embedded in dashboard.
- Portainer for container management.
- 17-command Telegram bot with multi-camera awareness and admin role gating.
- DVR recording with configurable retention (28-day default for recordings, 4-day for snapshots, 3-day for AI/Telegram clips).

### Requirements
- NVIDIA driver R555+ (CUDA 12.8) — required for Blackwell (RTX 50-series).
- Docker Engine + `nvidia-container-toolkit`.
- Linux (Ubuntu 22.04 / 24.04 / Debian 12) or Windows 11 + WSL2.

### Hardware tested
- Single-host with RTX 5070 Ti (16 GB) + RTX 3090 (24 GB) running Ubuntu 24.04 inside WSL2 mirrored networking on Windows 11.
- Single-GPU works fine — defaults assume 8–12 GB; smaller and larger tiers are available.

### Known limitations
- macOS not supported (CUDA-only inference path).
- Single-user authentication (one admin account, bcrypt-hashed, HMAC session cookies); no team / role model.
- LAN-only by design; expose via reverse proxy + `DASHBOARD_BEHIND_TLS=true` if needed.
- AI chat with Qwen 3 14B is reliable for single-purpose questions; compound multi-part questions can be muddled (see suggestion chips in the AI tab).

[Unreleased]: https://github.com/gammahazard/vision-labs-v2/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/gammahazard/vision-labs-v2/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/gammahazard/vision-labs-v2/releases/tag/v0.1.0
