# Vision Labs — Packaging & Distribution Plan

> **Goal:** Make Vision Labs runnable by a non-author on hardware ranging from a 6 GB consumer GPU up to a dual-card workstation, with a single shareable image set and a guided first-run.
>
> **Status:** Plan only. No code changes yet. Captured here so we can pick up pieces in any order.

---

## Table of Contents

1. [What we ship today vs. what users need](#1-what-we-ship-today-vs-what-users-need)
2. [Camera compatibility](#2-camera-compatibility)
3. [Hardware tier matrix](#3-hardware-tier-matrix)
4. [Phase A — Remove the Generate tab (foundational)](#4-phase-a--remove-the-generate-tab-foundational)
5. [Phase B — Hardware profiles + single-GPU support](#5-phase-b--hardware-profiles--single-gpu-support)
6. [Phase C — Pre-built images on a registry](#6-phase-c--pre-built-images-on-a-registry)
7. [Phase D — First-run setup wizard](#7-phase-d--first-run-setup-wizard)
8. [Phase E — Native installer (deferred)](#8-phase-e--native-installer-deferred)
9. [Open decisions](#9-open-decisions)

---

## 1. What we ship today vs. what users need

**Today's state:**

- 21 services across 13 images
- Two compose profiles (default + `cam2`), pre-defined cam2-cam5 slots
- Orchestrator auto-spawns camera services on Save (Phase 7b)
- Multi-camera-aware: registry, detector flags, event fan-out, AI tools, Grafana with per-camera labels
- Hardcoded to dual-GPU (5070 Ti + 3090) via `device_ids`
- First-time build is ~30 minutes
- Setup: `cp .env.example .env`, fill in 8-15 fields, `docker compose --profile cam2 up -d --build`

**Friction for a non-author user:**

1. **WSL2 + Docker Engine + NVIDIA Container Toolkit** install on Windows — multi-step, error-prone
2. **Hand-editing `.env`** — 15 fields, no defaults for camera URLs, no validation, secrets in plaintext
3. **First-build time** — 30 minutes is "I think it broke" territory for someone new
4. **GPU assumption** — dual-GPU split fails on single-GPU systems; no easy way to right-size
5. **Heavy on-demand workloads** (ComfyUI ~10 GB image + SDXL models, Ollama ~15 GB models) — bandwidth + VRAM gates we don't need for a security stack
6. **No discovery flow** — adding a camera assumes the user knows their RTSP URL syntax for their specific camera brand
7. **Default `admin/admin`** still ships, even though there's a forced-rotation flow

---

## 2. Camera compatibility

### Works out of the box

Any device that publishes a TCP-reachable RTSP stream the host can hit with `ffprobe`. Confirmed compatible:

- **Reolink** (any RLC/RLN series — `/h264Preview_01_main` + `/h264Preview_01_sub`)
- **Hikvision** (most models — `/Streaming/Channels/101` etc.)
- **Dahua / Amcrest** (`/cam/realmonitor?channel=1&subtype=0`)
- **Foscam** / **Tapo** / **Wyze with custom firmware**
- **Pi + mediamtx + USB webcam** (what `cam2` runs on this deployment)
- **Generic ONVIF cameras** that expose RTSP alongside ONVIF
- **RTSP-enabled doorbells** (Reolink, Unifi G4/G5)

### Doesn't work without a bridge

- **Ring**, **Nest**, **Arlo**, **Eufy** — closed ecosystems, no local RTSP
- **HomeKit Secure Video** — encrypted-to-iCloud, no LAN access
- **Cloud-only cameras** that don't expose an on-device stream
- **Cameras behind a NAT'd subnet** the host can't route to

### What the system actually needs from a camera

Two URLs ideally, one is fine:

| URL | Used for | Resolution | Bitrate |
|---|---|---|---|
| `rtsp_sub` (required) | Detection inference + DVR recording | 640×360 to 640×480 | ~512 kbps |
| `rtsp_main` (optional) | HD viewing in the dashboard | 1080p / 4K | as high as your bandwidth allows |

If the camera only has one stream, point `rtsp_sub` at it and leave `rtsp_main` blank. Detection will run on whatever resolution is published; expect higher GPU load if it's 1080p+.

### Cameras that need a transcoding bridge

Some cameras only output H.265 at high bitrates, or use proprietary codecs. For those, run **mediamtx** or **go2rtc** on the host or a tiny Pi to re-publish a downsized H.264 RTSP that Vision Labs consumes. This is the pattern `cam2` uses (Pi5 + Logitech C922 + mediamtx → `rtsp://192.168.5.45:8554/basement`).

---

## 3. Hardware tier matrix

**Core insight:** the only component that varies materially with VRAM is the **AI chat LLM**. Everything else (detection, face recognition, tracking, DVR, notifications, AI tools that aren't the chat itself) is designed to run on **≤2 GB VRAM** total — even on the small tier. Vehicle detection is per-camera-optional and stays off for indoor cameras automatically (see wizard flow in Phase D).

So the "tier" decision really comes down to: **how big a chat model do you want?** Detection works the same on every card.

Goal: support GPUs from 6 GB all the way up to dual-GPU workstations with one codebase, one compose file, three profiles.

### Current VRAM breakdown

| Component | Model today | VRAM | Notes |
|---|---|---|---|
| pose-detector | YOLOv8s-pose | ~500 MB | Per camera |
| vehicle-detector | YOLOv8s | ~500 MB | Per camera |
| face-recognizer | InsightFace buffalo_l | ~600 MB | Shared across cameras |
| ollama (chat) | Qwen 3 14B | **~9 GB** | Lazy-loaded, 5-min keep-alive |
| ollama (vision) | MiniCPM-V | ~5 GB | Lazy-loaded |
| comfyui | SDXL + LoRAs | **6-12 GB** | Only when generating |

Per-camera detectors scale linearly with camera count: 2 cams ≈ 3 GB just for detection.

### Proposed tiers

| Tier | Target GPU(s) | Pose | Vehicle | Face | Chat LLM | Vision | Image gen | Total VRAM |
|---|---|---|---|---|---|---|---|---|
| **`small`** | 6 GB single (GTX 1660 Ti, RTX 3050, RTX 2060) | YOLOv8n-pose | YOLOv8n | InsightFace (small) | Qwen 3 3B | none | **disabled** | ~4 GB |
| **`mid`** | 8-12 GB single (RTX 3060, RTX 4060, RTX 4070) | YOLOv8s-pose | YOLOv8s | InsightFace (buffalo_l) | Qwen 3 7B | optional MiniCPM-V | **disabled** | ~6-9 GB |
| **`full`** | 12+ GB single OR dual-GPU | current models | current | current | Qwen 3 14B | MiniCPM-V | optional | ~11+ GB |

### Single-GPU is the default; dual-GPU is the override

**Most users have one GPU.** The current dual-GPU split (`device_ids: ['0']` for detectors, `['1']` for ollama) is the author's unusual workstation; the v1 default should be **everything on GPU 0**, with a `docker-compose.dual-gpu.yml` overlay for users who actually have two cards.

Concretely:
- Base `docker-compose.yml`: all GPU services request GPU 0 (`device_ids: ['0']`)
- `docker-compose.dual-gpu.yml` overlay: shifts ollama + (legacy) comfyui to GPU 1
- README's "Quick start" assumes one GPU and does NOT mention the overlay
- Advanced README section: "If you have two GPUs, layer in `-f docker-compose.dual-gpu.yml`"

This also resolves a real bug: with one GPU and the current hardcoded `device_ids: ['1']` on ollama, ollama fails to start. We've been masking this by always running on the author's machine.

### Model swapping mechanics

All model paths are already env-overridable (we did this in the B3 cleanup):

- `MODEL_NAME=/models/yolov8n-pose.pt` (pose-detector)
- `MODEL_NAME=/models/yolov8n.pt` (vehicle-detector)
- `CHAT_MODEL=qwen3:3b` (constants.py)
- `VISION_MODEL=""` to disable vision (constants.py needs a small if-guard)
- `MATCH_THRESHOLD=0.5` (face-recognizer; lower → more recognitions, more false positives)

YOLO `.n` and `.s` models are tiny (~7 MB and ~22 MB respectively); they live in the `yolo-models` volume. Could pre-seed all four (`yolov8n.pt`, `yolov8n-pose.pt`, `yolov8s.pt`, `yolov8s-pose.pt`) at build time so profile switching is just an env-var change, no download.

---

## 4. Phase A — Remove the Generate tab (foundational)

The Generate tab in `/ai.html` is the heaviest, least-relevant feature for a security-camera product. Removing it unlocks the small-tier hardware story and dramatically simplifies the codebase.

### What it costs to keep

- ComfyUI image: ~10 GB pulled, custom nodes auto-installed at boot
- ~6-12 GB VRAM when active
- Custom model directory (`models/comfyui/`) with checkpoints, LoRAs, VAE, Ultralytics detectors — none auto-downloaded
- `gpu:generation_active` flag + pause logic in every detector
- `gpu:generation_lock` mutex
- `services/dashboard/routes/image_gen.py` (~500 LOC)
- `services/dashboard/static/generate.js` (1570 LOC)
- `services/dashboard/static/generate.css`
- ai.html Generate tab + Vision tab integrations
- Two AI tools (`schedule_image_generation`, etc.)
- 2 volumes (`comfyui-data`, `qnap-generations`)
- ~127 MB of comfyui-data at rest

### Removal steps (concrete file changes)

1. **`docker-compose.yml`**
   - Delete the `comfyui:` service block entirely (~40 lines)
   - Delete `comfyui-data` and `qnap-generations` volumes
   - Remove `COMFYUI_HOST` env from dashboard

2. **`services/dashboard/`**
   - Delete `routes/image_gen.py`
   - Delete `static/generate.js`, `static/generate.css`
   - From `static/ai.html`: remove the Generate tab button, the `#tabGenerate` panel, and references to `generate.js`/`generate.css`
   - From `server.py`: stop including `image_gen.router`
   - From `routes/ai_tools.py`: remove `schedule_image_generation` and `cancel_image_generation` tool defs + executors
   - From `pollers/comfyui_cleanup.py`: delete the file; remove its registration in `server.py`
   - From `routes/ai.py`: drop any Generate-tab status endpoints

3. **`services/comfyui/`** — delete the directory entirely

4. **`models/comfyui/`** — delete (or leave on disk; not referenced after compose change)

5. **Detectors** — remove `gpu:generation_active` pause logic in `pose-detector/detector.py`, `vehicle-detector/detector.py`, `face-recognizer/recognizer.py`. Simplifies the loops.

6. **Docs**
   - README: remove Image Generation section + the AI tools list cleanup
   - ARCHITECTURE.md: remove ComfyUI service entry, image gen section, gpu lock flags from Redis schema
   - PHASES.md: note Phase 7d "Generate tab removal" complete

### Test plan

- `docker compose up -d` succeeds with no ComfyUI service
- Dashboard loads, AI chat works without errors
- All other tabs work (Chat, Vision optional, DVR)
- Grafana doesn't show stale `gpu:generation_active` data
- Detectors don't log "GPU generation active" pauses

### Effort estimate

**4-6 hours of focused work.** Single mechanical pass, no logic to design.

---

## 5. Phase B — Hardware profiles + single-GPU support

Goal: one compose file that scales from a 6 GB card to a workstation, controlled by a single profile flag.

### Design

```yaml
# docker-compose.yml — additive overlays
# Default profile = "mid" works on 8-12 GB
# `--profile small` swaps in nano models + smaller LLM
# `--profile full` enables the current dual-GPU split
```

But compose profiles don't gracefully let you swap env vars per-profile. Better pattern: **separate `.env` files per tier** and a wrapper script:

```bash
# Pick a tier
cp tiers/.env.small .env

# Then standard compose
docker compose up -d
```

Or: use compose `extends` with `docker-compose.tier-small.yml` overlays. Cleanest.

### What changes per tier

| Variable | small | mid | full |
|---|---|---|---|
| `POSE_MODEL` | `/models/yolov8n-pose.pt` | `/models/yolov8s-pose.pt` | `/models/yolov8s-pose.pt` |
| `VEHICLE_MODEL` | `/models/yolov8n.pt` | `/models/yolov8s.pt` | `/models/yolov8s.pt` |
| `CHAT_MODEL` | `qwen3:3b` | `qwen3:7b` | `qwen3:14b` |
| `VISION_MODEL` | `` (disabled) | `minicpm-v` | `minicpm-v` |
| `NVIDIA_VISIBLE_DEVICES` (detectors) | `0` | `0` | `0` |
| `NVIDIA_VISIBLE_DEVICES` (ollama) | `0` (shared) | `0` (shared) | `1` |
| `TARGET_FPS` | `5` | `10` | `15` |

### Single-GPU support

Two paths:

1. **Reuse same GPU 0 for everything** — works as long as VRAM headroom exists. Detection inference is bursty; ollama eats steady VRAM when loaded. With 3 GB detectors + 5 GB ollama, you need 8+ GB and **no concurrent inference burst** (which we don't have today).

2. **Drop ollama entirely for small tier** — even simpler. AI chat just becomes unavailable. The dashboard already gracefully handles "ollama down" via the warmup poller's status messages.

Recommendation: small tier = no ollama, no vision, no gen. Just detection + tracking + faces + DVR + notifications. That's still a complete security system.

### Effort estimate

**1-2 days.** Mostly compose-overlay scaffolding + env-var threading + docs.

---

## 6. Phase C — Pre-built images on a registry

Skip the 30-minute first-build. Push images to GHCR (free public) or Docker Hub.

### What gets registry-published

- `ghcr.io/<owner>/vision-labs/camera-ingester:v1`
- `ghcr.io/<owner>/vision-labs/pose-detector:v1`
- `ghcr.io/<owner>/vision-labs/vehicle-detector:v1`
- `ghcr.io/<owner>/vision-labs/face-recognizer:v1`
- `ghcr.io/<owner>/vision-labs/tracker:v1`
- `ghcr.io/<owner>/vision-labs/recorder:v1`
- `ghcr.io/<owner>/vision-labs/dashboard:v1`
- `ghcr.io/<owner>/vision-labs/orchestrator:v1`

### Build pipeline

GitHub Actions workflow on tag push:

```yaml
on:
  push:
    tags: ['v*']
jobs:
  build-publish:
    strategy:
      matrix:
        service: [camera-ingester, pose-detector, vehicle-detector, ...]
    steps:
      - uses: docker/build-push-action@v5
        with:
          context: services/${{ matrix.service }}
          tags: ghcr.io/<owner>/vision-labs/${{ matrix.service }}:${{ github.ref_name }}
          push: true
```

### What users do

```bash
git clone <repo>
cd vision-labs
cp .env.example .env  # edit
docker compose up -d  # pulls instead of builds → 2-3 min instead of 30
```

### Tradeoffs

- Pros: dramatic UX improvement on first install; updates become `pull`
- Cons: need GitHub Actions or other CI to publish; need to commit to a versioning scheme; image sizes (pose/vehicle/face are each ~3-5 GB due to CUDA + onnxruntime)

### Effort estimate

**1 day** to set up the workflow + push the first images. Then auto-publishes on every tag.

---

## 7. Phase D — First-run setup wizard

Goal: zero-edit-of-files install. User runs `docker compose up -d`, hits `localhost:8080`, walks through a wizard, and the system is configured.

### Wizard flow

1. **Welcome** — explains what's about to happen.

2. **Hardware auto-detection** — run `nvidia-smi --query-gpu=name,memory.total --format=csv,noheader` inside a container we already trust (the orchestrator has it). Output drives the **recommended AI model** since that's the single biggest VRAM variable. Everything else (detection, faces, DVR, tracking) is designed to run on ≤2 GB VRAM regardless, so the AI model is the only "tier" decision a user really needs to think about.

   | Detected VRAM | Recommended chat LLM | Vision LLM | Notes shown to user |
   |---|---|---|---|
   | < 4 GB or no GPU | **No AI chat** | none | "Detection works fine; AI assistant disabled to fit your hardware" |
   | 4-7 GB | **Qwen 3 3B** (~2 GB) | none | "Lightweight chat — good for quick questions" |
   | 8-11 GB | **Qwen 3 7B** (~5 GB) | optional MiniCPM-V | "Smart chat + camera image analysis" |
   | 12-15 GB | **Qwen 3 14B** (~9 GB) | MiniCPM-V | "Full AI capabilities" |
   | 16+ GB or dual-GPU | **Qwen 3 14B** | MiniCPM-V | "Recommended config" |

   User can override (dropdown) but the default should be the safe pick for their card.

3. **Add your first camera** (optional — "I'll do this later" skips to step 5)
   - **RTSP URL** input, with **Test Connection** button (uses existing `/api/cameras/test-rtsp`)
   - **What type of camera is this?** radio:
     - 🏠 **Outdoor** — enables person + vehicle detection (driveway / front door / yard)
     - 🛋️ **Indoor** — enables person + face detection, **vehicle detection off** (basement / hallway / living room — cars don't appear)
     - 🚗 **Driveway/parking only** — vehicles + person, face detection off (saves GPU when faces aren't expected)
     - ⚙️ **Custom** — manually toggle each detector
   - This question writes the right `detect_persons` / `detect_vehicles` / `detect_faces` flags into the registry, so the camera spawns only the detectors it actually needs. Saves GPU even on the small tier.

4. **Notifications** — optional Telegram bot token + chat ID, or "Skip" → finish without notifications.

5. **Admin credentials** — pick username + password (replaces the admin/admin forced-rotation flow).

6. **Done** — lands on the dashboard, with a small panel suggesting next steps: add more cameras, enroll faces, set up zones.

### Where the data lands

- Hardware tier → writes to `.env` AND triggers `docker compose --profile <tier> up -d` for any services not yet running
- Camera → goes through normal POST `/api/cameras` flow (orchestrator brings it up)
- Telegram → writes to `.env`, restarts dashboard
- Admin → POST `/api/auth/initial-setup` (new endpoint)

### How the dashboard knows it's first-run

Check at startup: if `auth.db` has no admin user with a non-default password AND `cameras:registry` is empty AND `.env` is missing key values → redirect all routes to `/setup.html` until done.

### Effort estimate

**2-3 days.** New `setup.html` + `setup.js`, 4-5 new routes, a "setup gate" middleware, .env writer.

---

## 8. Phase E — Native installer (deferred)

Not blocking any earlier phase. Worth doing only if the project gets traction outside the author.

Approaches:

- **Windows MSI** that bundles WSL2 install, Docker Engine, NVIDIA Container Toolkit, our compose file. Probably 2-3 weeks of installer work.
- **Mac**: just Docker Desktop instructions in the README (Mac users have no NVIDIA so it's CPU-only anyway, which our small tier doesn't currently support)
- **Linux**: shell script wrapping `apt install docker.io nvidia-container-toolkit && docker compose up -d`

Realistic deferral: do this only if more than a handful of non-author users want it.

---

## 8a. Modularity & git hygiene

Two things to keep clean as we add features:

### Where things live

The code is already reasonably modular — each Docker service is a directory under `services/`, dashboard routes are split into one file per concern, and `contracts/` is the single source of truth for stream keys. But there are a few referenceability improvements worth making as part of packaging:

| Concern | Today | Improvement |
|---|---|---|
| **Per-service docs** | A docstring at the top of each Python entry-point | Add a one-page `services/<name>/README.md` per service: purpose, env vars, Redis touchpoints, how to run it standalone for debugging |
| **Frontend module map** | 13 JS files under `static/`, each ~100-700 lines | Add a `static/README.md` listing which JS files own which dashboard pages + how they share state (currently scattered window globals) |
| **Cross-service contracts** | `contracts/streams.py` documents stream keys; nothing centralizes the event payload shapes | Move per-event-type schemas into `contracts/events.py` (typed dicts) — both producers (tracker, face-recognizer) and consumers (event_renderer, ai_tools) reference them |
| **Config knobs** | Scattered across `.env.example`, `constants.py`, and 8+ services' `os.getenv` calls | One generated `docs/CONFIG.md` listing every env var, default, and which services read it. Auto-regen from a `grep os.getenv` script at release time |
| **Plan docs** | PHASES.md (history), REFACTOR_PLAN.md (history), PACKAGING_PLAN.md (this) | Keep PHASES as the running status doc; archive REFACTOR_PLAN once we hit v1 (it's largely done) |

None of this is blocking. The modularity is already there in the code; we're just adding signposts so a new contributor (or future-you, six months from now) can find things faster.

### Git workflow during packaging

The project is in a local git repo at `~/projects/vision-labs/.git` with ~10 prior commits. **No GitHub remote exists yet.** Recommended workflow as we start packaging work:

1. **Commit the current session's work as a baseline** before any packaging refactor. 46 files dirty right now — covers face_db refactor, navbar unify, modal, Phase 7b orchestrator, metrics relabel. Make this one big "v0.9 — pre-packaging baseline" commit so we have a clean rollback point.
2. **One commit per Phase A/B/C/D step** — small, reviewable, easy to revert.
3. **Add a GitHub remote** when ready to share — gives us issue tracking, releases, and a place to host the registry images (GHCR).
4. **Tag releases** (`v0.9-baseline`, `v1.0-pre-packaging`, `v1.0`, etc.) so users can pin to a known-good version.

The destructive `--remove-orphans` incident from earlier in 7b development is exactly the kind of thing tag-based rollback would save next time: `git checkout v0.9-baseline -- services/orchestrator/` undoes a bad change in seconds.

---

## 9. Open decisions

These don't block starting work, but writing them down so we can decide later:

| Decision | Options | Recommended |
|---|---|---|
| Where do registry images live? | GHCR (GitHub free), Docker Hub (free up to limits), self-hosted Harbor | **GHCR** — free, integrates with our git, no rate limits for public images |
| What versioning scheme? | semver from tags (`v1.0.0`), date-based (`2026.05.18`), commit-SHA-only | **semver** — predictable upgrades, supports security patches |
| Should small tier drop ollama entirely? | Yes (no AI chat at all) / No (run qwen3:3b shared on GPU 0) | **Yes, drop it.** Reduces VRAM floor from ~6 GB to ~4 GB. AI chat is nice-to-have, not core |
| Vision model in mid tier? | Always on / Optional toggle | **Optional toggle.** MiniCPM-V is 5 GB; some users won't want it |
| Where do model files come from? | Bake into image (huge images), download at first boot (~30 min first-run), download on-demand (lazy) | **Lazy download on first use** — fastest first-boot, models cached forever after |
| How do we handle camera registration in the wizard? | One camera required / Optional / "I have RTSP cameras" yes/no branch | **Optional.** User can add cameras later via /cameras.html |
| Default credentials? | Keep admin/admin + forced rotation / Force pick during setup / Random generate + show once | **Force pick during setup.** Removes the forced-rotation special case entirely |

---

## Suggested execution order

For shipping a v1 that someone else can actually install:

1. **Phase A first** (4-6h) — removes a service, removes a tab, removes ~2000 LOC. Strict cleanup, low risk. Foundation for everything else.
2. **Phase B** (1-2 days) — hardware profiles. Now anyone with a 6-12 GB GPU can run it.
3. **Phase C** (1 day) — registry images. First-install drops from 30 min to 3 min.
4. **Phase D** (2-3 days) — setup wizard. Zero file editing.
5. **Phase E** (only if needed) — native installer.

**Phases A + B + C = a sharable v1.0** that any technical user can stand up in ~10 minutes. Phase D makes it accessible to less technical users. Total effort to v1.0: roughly one focused week.

---

## Notes for future-me

- **Telegram bot is optional in every tier** — already gates on `TELEGRAM_BOT_TOKEN` being set
- **QNAP is already optional** via the overlay file pattern — keep that working
- **Multi-camera Phase 7b is done** — the auto-spawning orchestrator is the foundation of the user-friendly "Add camera" UX
- **Don't break the dual-GPU power-user path** — full tier should keep working exactly as today
- **Don't promise CPU-only support yet** — InsightFace + YOLO on CPU is 10-100× slower; would need a separate "CPU tier" only useful for stuck-without-GPU testing
