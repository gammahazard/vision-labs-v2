# Vision Labs — AI-Powered Security Camera System

> **Real hardware. Real-time inference. Fully self-hosted.**

A self-hosted, multi-camera AI security platform that processes live RTSP feeds through person detection, face recognition, vehicle tracking, and an LLM-powered chat assistant — all running locally via Docker Compose with zero cloud dependencies.

Built and tested on a dual-GPU workstation (RTX 5070 Ti + RTX 3090) running Ubuntu 24.04 inside WSL2 on Windows. Single-GPU works fine too — defaults are tuned for an 8–12 GB card; tiers are available for smaller and larger rigs.

---

## What you get

- **Person + face + vehicle detection** on every camera in real time (YOLOv8s-pose, InsightFace, YOLOv8s)
- **AI scene descriptions** on every Telegram alert (MiniCPM-V vision LLM)
- **19-tool AI assistant** (Qwen 3 14B) — query events, capture live snapshots (with auto vision-model description), set reminders, find DVR segments — all multi-camera aware
- **DVR recording** — 1-hour MPEG-TS segments, browseable through the dashboard with date + camera filters
- **Drawable zones** with per-time-of-day alert rules (always / night-only / log / ignore / dead zone)
- **Up to 20 cameras** out of the box (symmetric `cam1`–`cam20` slots, orchestrator-managed). The real cap is GPU VRAM, not the slot count — a 16 GB card with AI chat off comfortably handles 7+ cameras at 'n' detector models; a 24 GB 3090 with chat off pushes 12+. The wizard estimates a number for your hardware.
- **Prometheus + Grafana monitoring** embedded in the dashboard

---

## Architecture at a glance

```
Camera (RTSP) ──▶ Ingester ──▶ Redis Streams ──▶ Detectors (pose + vehicle + face)
                                              ──▶ Tracker ──▶ Events
                                              ──▶ Dashboard (WebSocket + REST)
                                                       │
                                                       ├─▶ Browser
                                                       ├─▶ Telegram Bot
                                                       └─▶ AI Assistant (Ollama)
```

Adding a camera in the UI = upsert into `cameras:registry` + a pub/sub trigger. The orchestrator hears it and brings the slot's services up via `docker compose --profile camN up -d`. The dashboard itself never touches the Docker socket.

For the full data flow and per-service responsibilities, see **[DETAILED_README.md §3](DETAILED_README.md#3-service-map)** and **[CONTEXT.md](CONTEXT.md)**.

---

## Quick install

### Linux (Ubuntu 22.04 / 24.04, Debian 12)

```bash
git clone <repo-url> vision-labs && cd vision-labs
bash scripts/install-linux.sh
```

The script installs Docker + nvidia-container-toolkit, builds all images, starts the stack, and tells you when the dashboard is up. Idempotent — safe to re-run. ~15–20 min on first install.

### Windows 11 (with NVIDIA GPU)

```powershell
# In an ELEVATED PowerShell (right-click -> Run as administrator):
powershell.exe -ExecutionPolicy Bypass -File .\scripts\install-windows.ps1
```

The script installs WSL2, writes a `.wslconfig` with mirrored networking, then prompts a reboot. After reboot, open the new Ubuntu terminal and run `bash scripts/install-linux.sh` from your cloned repo to finish.

### macOS

Not supported — the inference pipeline is CUDA-bound. Run Vision Labs on a Linux box or Windows + NVIDIA, then access the dashboard from your Mac via LAN.

### After install

Open `http://localhost:8080`, log in with `admin/admin` (forced password rotation on first login), then walk through the setup wizard: GPU probe → recommended hardware tier → add your first camera (ONVIF scan or manual RTSP URL) → done.

---

## Requirements

- **NVIDIA GPU** with driver supporting CUDA 12.8 (R555+). 6 GB VRAM minimum (small tier); 12+ GB recommended for AI chat. Apple Silicon / Intel iGPU / AMD GPU are not supported.
- **Docker Engine** (not Docker Desktop). On Windows, runs inside WSL2 Ubuntu — the installer sets this up for you.
- **RTSP-capable IP camera** (tested with Reolink RLC-1240A). ONVIF auto-discovery works with Reolink, Hikvision, Dahua, Amcrest, Axis, Unifi G-series. DIY RTSP setups (Pi + mediamtx, go2rtc, OBS) work via manual URL entry.
- **QNAP NAS** is optional — only needed if you want to offload DVR recordings to network storage.

---

## URLs after install

| Service | URL | Notes |
|---|---|---|
| Dashboard | http://localhost:8080 | Main UI. `admin/admin` on first run. |
| Portainer | https://localhost:9443 | Docker management UI |
| Grafana | http://localhost:3000 | System metrics (also embedded in dashboard) |
| Prometheus | http://localhost:9090 | Raw metrics |

---

## Learn more

- **[DETAILED_README.md](DETAILED_README.md)** — feature deep-dive, manual setup, env vars, dashboard pages, Telegram commands, backup/restore, hardware tiers
- **[CONTEXT.md](CONTEXT.md)** — full project context for developers: service-by-service responsibilities, Redis schema, orchestrator behavior, gotchas
- **[ARCHITECTURE.md](ARCHITECTURE.md)** — architectural reasoning: why services are split this way, Redis bus design, GPU placement
- **[docs/history/](docs/history/)** — historical planning docs (MANUAL_SETUP, PHASES, REFACTOR_PLAN, PACKAGING_PLAN)

---

## Scope + status

This is a **personal / portfolio project**, not a productized product. It runs in the author's home on a dual-GPU workstation and is documented to the level of "another developer could stand it up." A few things to set expectations:

- **LAN-only by design.** The dashboard is meant to be reached at `http://<host-ip>:8080` from devices on the same network. There is no built-in TLS terminator; if you want to expose it to the internet, put it behind a reverse proxy you trust (Caddy, nginx-proxy-manager, Cloudflare Tunnel) and set `DASHBOARD_BEHIND_TLS=true` so session cookies flip to `Secure`.
- **Single-user authentication.** One admin account, bcrypt-hashed password, HMAC-signed session cookies, brute-force-rate-limited login. No team/role model. Good enough for self-hosted home use; not designed for multi-tenant.
- **No support, no warranty.** MIT licensed (see [LICENSE](LICENSE)) — feel free to fork, learn from, or repurpose. Issues filed will be read but not necessarily fixed.

### What's optional

The system is overbuilt on purpose so the dual-GPU host has work to do. Several pieces can be removed cleanly:

- **AI chat (Qwen 3 14B + 19 tools)** — set `CHAT_MODEL=` (empty) in `.env`. Frees ~10 GB VRAM. The dashboard shows "AI chat disabled on this tier" and the rest of the system works unchanged.
- **MiniCPM-V vision scene descriptions** — set `VISION_MODEL=`. Saves ~5 GB VRAM. Telegram alerts still fire; they just don't include the auto-generated "person in a black hoodie walking left" sentence.
- **Telegram alerts** — leave `TELEGRAM_BOT_TOKEN` blank. The notification path gates on `is_configured()` and silently no-ops.
- **QNAP NAS** — opt-in via `QNAP_ENABLED=true` + the overlay compose file. Default install keeps recordings local.

### Known limitations

- macOS is not supported (CUDA-only inference).
- The orchestrator service has access to the Docker socket — it's the only one. The dashboard does not.
- The setup wizard's GPU probe is best-effort; on very new cards (Blackwell) verify CUDA 12.8+ before relying on auto-tier selection.

---

## License

[MIT](LICENSE) — see the LICENSE file for full text.
