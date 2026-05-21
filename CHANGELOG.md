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
- `/audit-repo` project-local Claude Code skill at `.claude/skills/audit-repo/`. Fans out subagents across four tracks (docs drift, code quality, architecture mapping, schema-drift between services) and writes five markdown reports to `audits/` (gitignored). Two-stage map → verify pattern with a "no finding without file:line evidence Read in this conversation" invariant on every verifier; mapper-output self-citation gate as defense against mapper hallucination. **First live run (2026-05-20) validated the skill on this codebase:** 5 latent NameError-class bugs (same family as the v0.1.1 bot_commands regression — `send_telegram` rate-limit constants, `schedule_reminder` parse_time/MAX_PENDING, `telegram_access` + `pollers/events` missing `TZ_LOCAL` imports), 1 path-traversal in `bot_commands/ask.py`, 2 schema-drift criticals (`hash:config:{cam}` and `hash:state:{cam}` missing-producer cases). The 5 NameErrors were specifically in code paths the existing `test_ai_tools_no_nameerror.py` misses due to fixture early-returns (the audit exposed test gaps in addition to product bugs). Pre-existing bugs left for a follow-up cleanup branch. Skill is **expensive** — expect ~30-40 minutes wall-clock and 100+ subagent dispatches per full run; per-account session rate limits may force the drift track into batched verifiers (hard rules preserved per-claim within batches). See design spec at `docs/superpowers/specs/2026-05-20-audit-repo-skill-design.md` and implementation plan at `docs/superpowers/plans/2026-05-20-audit-repo-skill.md`.
- `.claude/settings.json` — project-shared permission allowlist (currently just `Bash(gh run watch *)` for CI run monitoring). Per-user overrides go in the gitignored `.claude/settings.local.json`.

### Fixed
- Pose + vehicle detectors used wall clock (`time.time()`) for inference duration, so NTP corrections on WSL2 host-resume could produce negative `inference_ms` values that pulled the Grafana "YOLO Inference Time" mean below zero (visible as -2s spikes / -7.25s means on hour-zoom views). Switched both detectors to `time.monotonic()`. Requires rebuilding the affected detector images.
- `routes/notifications/frame.py` `build_clip()` opened a fresh Redis connection on every call instead of reusing the shared `ctx.r_bin`. Each Telegram clip + AI `capture_clip` request leaked a TCP connection; now uses the shared client.

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
