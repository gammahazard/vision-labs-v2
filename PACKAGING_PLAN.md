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
6a. [Phase C.2 — ONNX migration for detectors (optional)](#6a-phase-c2--onnx-migration-for-detectors-optional-post-c)
7. [Phase D — First-run setup wizard](#7-phase-d--first-run-setup-wizard)
7a. [Phase D.5 — Network camera discovery (ONVIF)](#7a-phase-d5--network-camera-discovery-onvif)
8. [Phase E — Native installer](#8-phase-e--native-installer)
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

Some cameras only output H.265 at high bitrates, or use proprietary codecs. For those, run **mediamtx** or **go2rtc** on the host or a tiny Pi to re-publish a downsized H.264 RTSP that Vision Labs consumes. The author's `cam2` slot uses this pattern (Pi5 + Logitech C922 + mediamtx → `rtsp://<pi-ip>:8554/<stream-name>`).

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

### Removal steps (audit-corrected — every file that references ComfyUI / generation)

This list was rebuilt by grepping `comfyui|COMFYUI|generation_active|generation_lock|schedule_image_generation|image_gen|GPU_PAUSE_KEY` across the repo. Misses from earlier drafts are flagged ⚠.

1. **`docker-compose.yml`**
   - Delete the `comfyui:` service block entirely (~40 lines)
   - Delete `comfyui-data` and `qnap-generations` volumes
   - Remove `COMFYUI_HOST` env from dashboard

2. **`contracts/streams.py`** ⚠ *(was missed)*
   - Delete `GPU_PAUSE_KEY` constant
   - Delete `GPU_LOCK_KEY` if defined here too

3. **`services/dashboard/`**
   - Delete `routes/image_gen.py`
   - Delete `static/generate.js`, `static/generate.css`
   - From `static/ai.html`: remove the `<link rel="stylesheet" href="generate.css">` (line 12), the `<button class="model-tab" data-tab="generate">` (line 109), the entire `<div class="generate-panel">` block (~lines 231-330+), and any `<script src="generate.js">` tag
   - From `server.py`: stop including `image_gen.router`; remove `comfyui_cleanup` poller registration
   - From `routes/ai_tools.py`: remove `schedule_image_generation` and `cancel_image_generation` tool defs + executors
   - From `pollers/comfyui_cleanup.py`: delete the file
   - From `pollers/__init__.py`: remove the import line
   - From `routes/ai.py`: drop any Generate-tab status endpoints (grep for `image_gen`, `generation_active`)
   - From `routes/metrics.py:194,385` ⚠ *(was missed)*: remove the two `r.exists("gpu:generation_active")` calls — they expose the flag as a Prometheus metric
   - From `constants.py` ⚠ *(was missed)*: remove `COMFYUI_HOST` and any image-gen settings

4. **`services/comfyui/`** — delete the directory entirely

5. **`models/comfyui/`** — delete (or leave on disk; not referenced after compose change)

6. **Detectors** — remove `GPU_PAUSE_KEY` import and pause-loop block:
   - `services/pose-detector/detector.py:50` (import), `306-314` (pause loop)
   - `services/vehicle-detector/detector.py:50` (import), `221-229` (pause loop)
   - `services/face-recognizer/recognizer.py:57` (import), `740` (pause check)

7. **Docs**
   - `README.md`: remove Image Generation section + the AI tools list cleanup
   - `ARCHITECTURE.md`: remove ComfyUI service entry, image gen section, gpu lock flags from Redis schema
   - `MANUAL_SETUP.md` ⚠ *(was missed)*: remove ComfyUI setup steps
   - `PHASES.md`: note Phase 7d "Generate tab removal" complete
   - `REFACTOR_PLAN.md` ⚠ *(was missed)*: archive any ComfyUI-related decisions to a historical-context section

### Safe removal order (so imports never break mid-edit)

The order matters because `GPU_PAUSE_KEY` is imported from `contracts/streams.py` by three services. Wrong order → the dashboard or detectors fail to start in the middle of the refactor.

1. First: remove `GPU_PAUSE_KEY` *usages* in detectors + dashboard (steps 3, 6 above), leave the constant defined.
2. Verify: `docker compose up -d --build` — everything starts.
3. Then: delete the constant from `contracts/streams.py` and the ComfyUI service block.
4. Verify again.
5. Last: docs + delete the comfyui/ directory + models/comfyui/.

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

### Single-GPU support — actual capacity math

Earlier drafts hand-waved "as long as VRAM headroom exists." Doing the math reveals a more honest picture:

**Per-camera detector cost (one camera spawned):**

| Process | CUDA context | Model | Total per process |
|---|---|---|---|
| pose-detector | ~400 MB | YOLOv8s ~500 MB | **~900 MB** |
| vehicle-detector | ~400 MB | YOLOv8s ~500 MB | **~900 MB** |
| face-recognizer | ~400 MB | buffalo_l ~600 MB | **~1.0 GB** |
| **Total per camera** | | | **~2.8 GB** |

Small tier with nano models drops this to ~2.0 GB/camera.

**Concurrent-load slot estimates (mid tier, Qwen 3 7B chat loaded ≈ 5 GB):**

| GPU | Total VRAM | Headroom after chat | Realistic slots |
|---|---|---|---|
| 6 GB single (small tier, no chat) | 6 | 6 | **2 cameras** (nano models) |
| 8 GB single | 8 | 3 | **1 camera** |
| RTX 3060 / 4060 (12 GB) | 12 | 7 | **2 cameras** |
| RTX 4060 Ti (16 GB) | 16 | 11 | **3 cameras** |
| RTX 3090 / 4090 (24 GB) | 24 | 19 | **6-7 cameras** |
| Dual-GPU (12+12) | 24 effective | chat on GPU 1 isolated | **4 cameras on detector GPU** |

**Honest take:** mid tier on an 8 GB single GPU = **1 camera max if chat is loaded**. Earlier draft language ("8-12 GB single") undersold this. The wizard must surface estimated camera capacity, not just a tier label.

### GPU contention on a single card

When ollama generates a chat response on the same GPU as the detectors, ollama steals SMs and detection latency spikes. The author's dual-GPU rig has never seen this. **Untested risk on single-GPU systems — needs an empirical test before claiming "works on a 3060."** Mitigations if it turns out to be bad:

- Tighten ollama keep-alive from 5 min → 30 s (faster eviction when idle)
- Make detection `TARGET_FPS` tier-dependent (small tier = 3 fps to leave headroom)
- Worst case: small tier drops ollama entirely, AI chat unavailable. Dashboard's warmup poller already handles "ollama down" gracefully.

### Author's machine doesn't lose its dual-GPU setup

After Phase B reshuffles the base file to GPU 0 everywhere, the author's local checkout needs to opt back into the dual-GPU layout. Two options:

- Add to author's `.env`: `COMPOSE_FILE=docker-compose.yml:docker-compose.dual-gpu.yml`
- Document this in `MANUAL_SETUP.md` (not just README) since that's the doc actively used on this machine

Without this, the next `docker compose up -d` after Phase B will dump everything on the 5070 Ti and OOM ollama.

### What changes per tier (confirmed)

Recommendation locked: **small tier drops ollama entirely** (open decision #3, recommended option). Reduces small-tier VRAM floor from ~6 GB to ~4 GB. The dashboard already handles "AI chat unavailable" via the warmup poller — needs a one-line addition to show "Disabled on this hardware tier" instead of "Warming up forever."

### Effort estimate

**1-2 days.** Compose-overlay scaffolding + env-var threading + dashboard tier-aware "chat disabled" message + docs.

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
- Cons: need GitHub Actions or other CI to publish; need to commit to a versioning scheme; image sizes are bigger than they should be

### Image-size honesty check

Earlier draft promised "30 min → 3 min." That's wrong without further work:

- pose-detector + vehicle-detector + face-recognizer each ship CUDA + PyTorch + ultralytics ≈ **3-5 GB each**
- 7-8 services total → **25-40 GB of pulls on a fresh install**
- At typical home bandwidth (100 Mbps) that's **35-55 minutes**, not 3

So Phase C as originally scoped just trades "build time" for "download time" with little net win. To actually deliver fast first-install, Phase C must include:

1. **Shared base image** (`vision-labs-base:cuda12.4-pytorch2.4`) used by all detector services. Docker dedups shared layers, so pulling 4 services that share a 3 GB base = 1 download of the base + 4 small deltas instead of 4 × 3 GB.
2. **Strip dev dependencies**: kill ultralytics' training/export extras, remove CPU-only torch wheels, drop pip caches in final layer.
3. **Consider onnxruntime-gpu instead of torch** for the two YOLO detectors. Inference-only, ~10× smaller image. Bigger lift — needs model conversion to ONNX. Defer to a Phase C.2 if Phase C alone doesn't hit a tolerable size.

**Realistic target after image work: ~8 GB total pull, ~10-15 min on 100 Mbps.** Still not 3 min, but no longer "go make dinner."

### Effort estimate

**1 day** to set up the workflow + push the first images. **+1 day** for base-image dedup + dependency strip. ONNX migration (Phase C.2) is another 2-3 days if we go there.

### Phase C — actually shipped (post-implementation notes)

Measured savings after layer dedup (from `docker image inspect`, May 2026):

| Image | Before Phase C | After Phase C | Saved |
|---|---|---|---|
| pose-detector | 18.3 GB | **7.4 GB** | 11 GB |
| vehicle-detector | 18.3 GB | **7.4 GB** | 11 GB |
| face-recognizer | 10.2 GB | **3.6 GB** | 6.5 GB |
| Slot copies (cam2/cam3 per service) | 18.3 GB each | **6.5 GB each** | 12 GB each |
| **Total stack disk** | ~140 GB | **~55 GB** | **~85 GB** |

Fresh `docker pull` from GHCR after Phase C completion: ~14 GB total (one base + per-service deltas). Earlier draft promised "8 GB" — that would require Phase C.2 (ONNX migration) on top to hit. 14 GB is still a 6× reduction from the unsplit baseline; honestly call this "fast enough for v1."

What landed:
- `services/base/Dockerfile` — shared CUDA 12.8 + cuDNN + Python 3.11 + system deps + numpy/opencv/redis (~3.2 GB)
- Pose / vehicle / face detector Dockerfiles rewritten as `FROM vision-labs-base` thin layers
- Obsolete `requirements.txt` files deleted (deps moved inline in Dockerfiles)
- `scripts/build.sh` — builds base first, then `docker compose build` finds it
- `docker-compose.registry.yml` overlay — `pull` mode, swaps every build directive for `image: ghcr.io/...`
- `.github/workflows/publish-images.yml` — tag-push triggers parallel build + GHCR publish for all 8 services + base

---

## 6a. Phase C.2 — ONNX migration for detectors (INVESTIGATED, NOT WORTH DOING)

**Status:** Empirically benchmarked May 2026 — the claimed VRAM savings don't materialize on modern PyTorch + onnxruntime-gpu. **Not pursuing.**

### What we tested

Loaded YOLOv8s-pose into a fresh container on an idle GPU twice — once via PyTorch 2.11 (current setup), once via onnxruntime-gpu 1.22 (proposed setup). Measured GPU memory after warm-up via `nvidia-smi`:

| Setup | Detector overhead on GPU |
|---|---|
| PyTorch + YOLOv8s-pose | 284 MiB |
| ONNX Runtime + YOLOv8s-pose | 299 MiB |
| **Delta** | **-15 MiB (ONNX slightly bigger)** |

Why the original "~200 MiB savings" claim was wrong: the overhead breakdown for either framework is ~250-300 MiB CUDA context + ~50 MiB cuDNN cache + ~50 MiB model weights + small runtime. Both frameworks pay the same CUDA + cuDNN tax; the framework-specific bits are too small to matter on a Blackwell/Ampere setup.

The image-size win is also smaller than originally pitched: Phase C's shared base image already dedupes the PyTorch layer across 3 detector services + slot copies, so the marginal save from ripping torch out would be ~1-2 GB total, not ~3 GB per image.

### What it would cost

- **~150-200 LOC** of manual YOLO output decoding (NMS, bbox, keypoint extraction) to replace ultralytics' `.boxes / .keypoints` API
- **Numerical regression risk** — small confidence drift between PyTorch/ONNX exports, easy to miss without a careful side-by-side validation
- **Ongoing maintenance cost** — every new ultralytics/YOLO version needs re-export + revalidation

Net: zero VRAM benefit, ~1-2 GB image-size benefit, real regression risk. Skipping.

### Earlier version of this section (kept for historical context)

### What ONNX is

ONNX (Open Neural Network Exchange) is a portable model file format. ONNX Runtime is the inference engine that runs those files — smaller, faster, inference-only (no training code), and already used by `face-recognizer` today (InsightFace ships ONNX models internally).

### What it buys us

| Metric | PyTorch (today) | ONNX Runtime | Win |
|---|---|---|---|
| Detector image size | ~3-5 GB | ~500 MB-1 GB | **~3 GB lighter per image** |
| VRAM per detector process | ~900 MB | ~700 MB | ~200 MB |
| Inference speed | baseline | typically 1.5-2× | Yes |

On a 6 GB single-GPU user running 3 cameras, the ~600 MB VRAM savings (3 detectors × 200 MB) is the difference between "tight" and "comfortable."

### What it costs

- Two services to migrate: `pose-detector` and `vehicle-detector`. `face-recognizer` is already ONNX.
- Conversion: `model.export(format='onnx')` via ultralytics — one-liner per model.
- Code change: ~200 LOC across both detectors — swap the PyTorch inference call for `onnxruntime.InferenceSession.run()`.
- **Numerical drift validation:** PyTorch and ONNX outputs can differ by ~1% in float operations. Need a side-by-side comparison run on a fixed test video to confirm detection quality holds. ~half-day of validation.

### Risks

- Detection-quality regression if drift is bigger than expected. Mitigated by side-by-side test before merge.
- Two inference code paths during migration if we don't commit fully. Plan: convert in one PR, no toggle.
- Some ultralytics post-processing (NMS, keypoint decoding) has to be reimplemented in NumPy or moved to onnxruntime's `onnxruntime-extensions`. Adds modest complexity.

### Effort estimate

**2-3 days** including validation. Skip if Phase C alone delivers a tolerable image-size story (~8 GB total pull).

---

## 7. Phase D — First-run setup wizard

Goal: zero-edit-of-files install. User runs `docker compose up -d`, hits `localhost:8080`, walks through a wizard, and the system is configured.

### Wizard flow

1. **Welcome** — explains what's about to happen.

2. **Hardware auto-detection** — earlier draft said "run nvidia-smi inside the orchestrator." That doesn't work: the orchestrator image is `docker:24-cli` (Alpine) with no `nvidia-smi` and no NVIDIA runtime.

   **Corrected approach:** the wizard backend asks the orchestrator (which has the Docker socket) to spawn a one-shot probe container:
   ```
   docker run --rm --gpus all nvidia/cuda:12.4-base-ubuntu22.04 \
     nvidia-smi --query-gpu=index,name,memory.total --format=csv,noheader
   ```
   Output is parsed into a list of GPUs. Adds a one-time ~200 MB pull on first wizard run. If the command fails (no NVIDIA, no GPU, driver issue), the wizard falls back to "We couldn't detect a GPU — pick a config manually" with a clear explanation.

   Output drives the **recommended AI model** since that's the single biggest VRAM variable. Everything else (detection, faces, DVR, tracking) runs on ≤2 GB VRAM regardless.

   | Detected VRAM | Recommended chat LLM | Vision LLM | Notes shown to user |
   |---|---|---|---|
   | < 4 GB or no GPU | **No AI chat** | none | "Detection works fine; AI assistant disabled to fit your hardware" |
   | 4-7 GB | **Qwen 3 3B** (~2 GB) | none | "Lightweight chat — good for quick questions" |
   | 8-11 GB | **Qwen 3 7B** (~5 GB) | optional MiniCPM-V | "Smart chat + camera image analysis" |
   | 12-15 GB | **Qwen 3 14B** (~9 GB) | MiniCPM-V | "Full AI capabilities" |
   | 16+ GB or dual-GPU | **Qwen 3 14B** | MiniCPM-V | "Recommended config" |

   User can override (dropdown) but the default should be the safe pick for their card.

   **Also show estimated camera capacity** based on the formula `floor((VRAM_GB - chat_VRAM - 1_GB_buffer) / 2.8)`. So a 12 GB / 7B-chat user sees "Estimated camera capacity: 2-3 cameras (more if you skip vehicle detection on indoor cams)." This is more honest than a tier label and helps users plan.

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

Earlier draft proposed a three-signal check (auth.db + registry + .env). That's fragile — wiping data to debug retriggers the wizard. **Corrected approach:** one explicit signal.

- A small JSON file at `/data/setup.json` (in a dedicated `setup-state` named volume) with `{"completed_at": "<iso8601>", "version": 1}`.
- Wizard writes this on its final step.
- Dashboard startup checks for this file's existence. If missing → redirect all routes to `/setup.html` (except the wizard's own routes).
- To re-run the wizard intentionally, the user deletes the file (`docker volume rm vision-labs_setup-state`). Documented.

### Camera-tab parity (post-Phase-D)

Once the wizard's network-scan flow exists (see Phase D.5), the same scan-and-add UI should also be available on the **Cameras tab** for adding cameras after initial setup. Same backend endpoint, same UI component reused. Plan accordingly when building Phase D.5 — don't hard-code it as a wizard-only flow.

### Effort estimate

**2-3 days.** New `setup.html` + `setup.js`, 4-5 new routes, a "setup gate" middleware, .env writer, orchestrator hook for GPU probe.

---

## 7a. Phase D.5 — Network camera discovery (ONVIF)

**Goal:** in the wizard *and* the cameras tab, let the user click "Scan my network" instead of pasting an RTSP URL. We find ONVIF-compatible cameras on the LAN, the user picks one, enters credentials, we resolve the working RTSP URL and add it.

### Why it's its own phase, not part of Phase D

Two reasons:

1. **WSL2 multicast is untested.** ONVIF WS-Discovery uses UDP multicast (`239.255.255.250:3702`). Default Docker bridge networking drops it. `network_mode: host` works on bare Linux, and *theoretically* works on WSL2 mirrored mode (which we already require for phone access on the LAN). Theory isn't enough — we need an empirical test before promising this works.
2. **Manual entry must ship first.** The wizard cannot block on discovery. If we tie them together and discovery proves flaky, the wizard ships late.

### How discovery works

UDP multicast probe to `239.255.255.250:3702`. ONVIF-compliant cameras reply with their device-service URL. We then make authenticated SOAP calls (`GetDeviceInformation`, `GetStreamUri`) to retrieve manufacturer/model and the actual RTSP URL for each stream profile.

### Architecture

- **New service `services/discovery/`** — tiny Python service, ~150 LOC, using `wsdiscovery` + `onvif-zeep`. Not a long-running daemon — spawned on demand.
- **Endpoint `POST /api/discovery/scan`** in dashboard. Calls into orchestrator (which has Docker socket) to run a one-shot:
  ```
  docker run --rm --network host visionlabs/discovery scan --timeout 8
  ```
  Returns JSON: `[{ip, manufacturer, model, device_url}]`.
- **Endpoint `POST /api/discovery/get-stream-uri`** — given `{device_url, username, password}`, runs the ONVIF SOAP call and returns `{rtsp_uri, profile_name, resolution}`.
- **Wizard flow:**
  1. User clicks "Scan my network."
  2. ~8s scan; show found cameras as cards.
  3. User clicks a card; prompted for username/password (with brand-specific hints).
  4. `GetStreamUri` runs; if it succeeds, RTSP URL prefills the existing camera form; user clicks Add.
  5. If anything fails, fall through to manual entry. **Discovery is a shortcut, never a gate.**
- **Cameras tab reuse:** the same Scan button appears at the top of the cameras list, opening the same flow inline.

### What this won't find

- Reolink cameras with ONVIF disabled in firmware (user has to enable it in the Reolink app first)
- Hikvision OEMs that strip ONVIF for licensing
- Cameras behind a router/mesh that blocks LAN multicast
- Pi + mediamtx setups (custom RTSP, no ONVIF) — these stay manual-entry

### Empirical test gate

Before committing development time:

1. From a Linux container with `network_mode: host` on this WSL2 host, run `wsdiscovery` against the LAN.
2. If we see the basement-Pi or any of the ringed phones/Reolinks: green light, build it.
3. If we see nothing: drop the feature, document "auto-discovery not supported on WSL2 today; use manual entry."

### Result — May 2026: multicast gate failed, but unicast scan works fine

**First attempt: multicast — failed.** Tested ONVIF WS-Discovery and SSDP/UPnP from a `--network host` container on the dev WSL2 host (WSL 2.6.3.0, mirrored mode). Both returned 0 responders even after adding Hyper-V firewall allow rules for UDP 1900/3702 and using explicit `IP_ADD_MEMBERSHIP` with bind to the LAN interface. Eventually got SSDP to return a few responders but the Reolink (with ONVIF enabled) never replied to multicast — and many home networks block multicast at the router anyway. Multicast on WSL2 isn't reliable enough to ship.

**Second attempt: unicast subnet scan — green light.** Sent the same WS-Discovery Probe SOAP envelope as unicast UDP to every IP in the local /24. The Reolink on the dev LAN responded with 1455 bytes of ONVIF metadata in 2 seconds. Worked on the first try, requires no firewall changes, no protocol assumptions about multicast.

**Decision:** un-drop Phase D.5. Ship the unicast scanner instead of the multicast probe.

### Implementation that actually shipped

- `helpers/onvif_discovery.py` — given a CIDR, fan out WS-Discovery probes as unicast UDP to every host in parallel (semaphore=50, per-IP timeout 2s, total ~5-10s for a /24). Parses XAddrs + Scopes from SOAP response.
- `routes/cameras.py` — new `POST /api/cameras/discover` runs the scan, `POST /api/cameras/onvif-stream-uri` does the SOAP GetProfiles + GetStreamUri dance with WSSE auth to retrieve RTSP URLs.
- `routes/setup.py` — `POST /api/setup/discover-cameras` thin wrapper that reuses the same endpoint inside the wizard's exempt path list.
- `setup.html / setup.js / setup.css` — wizard step 3 now has a "Scan my network" button + result cards + credential modal. Manual RTSP entry collapsed into a `<details>` block as the fallback.
- `cameras.html / cameras.js` — same scan UI added to the cameras tab so users can re-discover after adding more devices.
- Auto-CIDR detection: tries CAMERA_IP env, then parses RTSP_SUB/RTSP_MAIN env, then `connect(8.8.8.8)` socket trick (rejected if it returns a Docker bridge range like 172.17/16).

### What it won't find (worth documenting in the wizard hint text)

- DIY RTSP setups (Pi + mediamtx, Pi + ffmpeg, go2rtc, OBS server) — these speak RTSP but not ONVIF.
- Cameras with ONVIF disabled in firmware (Reolink default state, some Hikvision OEMs). User has to enable it in the camera's app before scanning.
- Cameras on a different subnet — the wizard accepts a custom CIDR input for that.
- Cloud-only cameras (Ring, Nest, Arlo) — no LAN protocol at all.

### Effort estimate (actual)

**~1 day** as estimated, modulo the multicast detour. Empirical multicast testing actually took longer than the build itself; the unicast switch was straightforward once the test gate flipped.

---

## 8. Phase E — Native installer

The earlier draft was vague ("deferred, do if traction"). Concrete reality, per-OS:

### Linux — viable, smallish

A shell script (or `.deb` / `.rpm`) that:

1. Installs `docker.io`, `docker-compose-plugin`, `nvidia-container-toolkit` (the toolkit is the main NVIDIA-driver gotcha).
2. Drops a `systemd` unit that runs `docker compose up -d` on boot.
3. Pre-pulls the Phase C base images.
4. Opens `http://localhost:8080` in the user's browser at the end.

**Effort:** 3-5 days of polish + testing on Ubuntu 22.04 / 24.04, Debian 12, Fedora 40.

### Windows — viable but multi-prompt

There is **no truly one-click path on Windows** because:

- WSL2 install needs admin elevation + Microsoft Store
- A reboot is required after WSL install
- Docker Engine + NVIDIA Container Toolkit install inside WSL2 needs `sudo` inside the WSL VM
- NVIDIA drivers on Windows must be the WSL-CUDA-enabled version (most modern Game Ready drivers are, but it's a check)

A realistic MSI/exe (Inno Setup or WiX):

1. Checks for an NVIDIA GPU + recent driver. Warns and exits if missing.
2. Installs WSL2 (`wsl --install` does this). One reboot.
3. After reboot: installs Ubuntu 22.04 WSL distro, drops a setup script in `/opt/vision-labs/`, runs it to install Docker + NVIDIA toolkit + pull images.
4. Adds a Start Menu shortcut: "Vision Labs" → opens browser to `http://localhost:8080`.
5. Auto-start: a Windows service that runs `wsl -d Ubuntu -- docker compose -f /opt/vision-labs/docker-compose.yml up -d` at boot.

**Effort:** 2-3 weeks of installer engineering, including testing on Win10 + Win11. Updates also non-trivial (replacing running containers, preserving data volumes).

### macOS — explicitly not supported

The whole inference pipeline is CUDA-bound. Apple Silicon has no CUDA. MPS-backed YOLO + InsightFace exists but inference is 5-20× slower — unusable for live multi-camera. CPU-only is 10-50× slower.

Reasonable v1 stance: "macOS is not a supported platform. Run Vision Labs on a Linux box or a Windows machine with an NVIDIA GPU and access the dashboard from your Mac via the LAN."

If we ever want to *change* this, the path is rewriting the detector pipeline on `coreml` / `onnxruntime` with Metal — that's a multi-month project, not part of v1 packaging.

### Suggested order if/when we do this

1. **Linux script first** (3-5 days). Easiest payoff, covers most likely production users.
2. **Windows MSI** (2-3 weeks) — only if there's clear demand outside the author.
3. **macOS** — never, until/unless we rework inference for Metal.

Don't block v1 ship on any of this. Pre-built images (Phase C) + setup wizard (Phase D) get the install down to "clone the repo + `docker compose up -d` + open browser" which is already a fine v1 story for technical users.

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

## Suggested execution order (audit-corrected)

For shipping a v1 that someone else can actually install:

1. **Phase A** (4-6h) — remove Generate tab + ComfyUI. Strict cleanup, low risk. Foundation for everything else.
2. **Phase B** (1-2 days) — single-GPU default + tier env files + dashboard "chat disabled" message + dual-GPU overlay for the author's machine.
3. **Phase C** (1-2 days) — registry images + shared base image dedup + dependency strip. Target ~8 GB total pull, not 25-40 GB.
4. **Phase C.2** (2-3 days, *optional*) — ONNX migration for pose + vehicle detectors. Smaller images, ~200 MB less VRAM per detector. Skip if Phase C alone is enough.
5. **Phase D** (2-3 days) — setup wizard with manual camera entry. Zero file editing.
6. **Phase D.5** (~1 day, *gated on a multicast test*) — ONVIF network discovery, in both wizard and cameras tab.
7. **Phase E** (only if there's demand) — Linux install script (3-5 days), Windows MSI (2-3 weeks). macOS not supported.

**Phases A + B + C + D = sharable v1.0** that any technical user can stand up in ~15 minutes. D.5 reduces the "I have to find my camera's RTSP URL" friction. Phase E makes it a true product for non-technical users.

**Total effort to v1.0:** roughly **1.5 focused weeks** of development (was undersold as "one week" in earlier draft — Phase C image-size work and the dashboard-chat-disabled message add ~1-2 days each).

---

## Notes for future-me

- **Telegram bot is optional in every tier** — already gates on `TELEGRAM_BOT_TOKEN` being set
- **QNAP is already optional** via the overlay file pattern — keep that working
- **Multi-camera Phase 7b is done** — the auto-spawning orchestrator is the foundation of the user-friendly "Add camera" UX
- **Don't break the dual-GPU power-user path** — full tier should keep working exactly as today. After Phase B, this machine needs `COMPOSE_FILE=docker-compose.yml:docker-compose.dual-gpu.yml` in its `.env`.
- **Don't promise CPU-only support yet** — InsightFace + YOLO on CPU is 10-100× slower; would need a separate "CPU tier" only useful for stuck-without-GPU testing.
- **Network discovery is a Phase D.5 nice-to-have, not Phase D blocker** — wizard ships with manual entry working. Discovery is also wanted in the cameras tab post-setup, so reuse the component.
- **Single-GPU contention is empirically untested** — when ollama is loaded on the same card as detectors, detection latency may spike. Test on a borrowed 12 GB single-GPU box before Phase B is "done."
- **macOS will not be supported in v1** — communicate this honestly. Mac users access the dashboard from a Linux/Windows host via the LAN.
- **Hardware requirements need to be honest in the README** — "Linux or Windows with an NVIDIA GPU, 6 GB VRAM minimum. Intel iGPU / AMD APU / Apple Silicon / no-GPU systems are not currently supported. Coral / OpenVINO support is not on the v1 roadmap."
- **The "typical home" sweet spot is 1-3 cameras on 6-12 GB.** That's the demographic the wizard's recommendations should optimize for, not the author's 4-camera dual-GPU rig.
