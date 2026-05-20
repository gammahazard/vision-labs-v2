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

### Fixed
- Pose + vehicle detectors used wall clock (`time.time()`) for inference duration, so NTP corrections on WSL2 host-resume could produce negative `inference_ms` values that pulled the Grafana "YOLO Inference Time" mean below zero (visible as -2s spikes / -7.25s means on hour-zoom views). Switched both detectors to `time.monotonic()`. Requires rebuilding the affected detector images.

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
