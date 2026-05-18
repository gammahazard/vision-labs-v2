# Vision Labs — Manual Setup Checklist

> Things only you can do (hardware, network admin, vendor accounts, host OS install). Work through these at your own pace; I'll handle the code/config side once we've got the inputs we need.
>
> **Status (May 2026):** Sections 7 and 8 are **DONE** on the current host — Docker Engine + NVIDIA Container Toolkit are installed inside WSL2 Ubuntu 24.04, and the project lives at `~/projects/vision-labs` on ext4. Kept here for reference if you ever rebuild the host or set this up on another machine.
>
> **Tip:** Fill in the "Values to collect" block at the top as you go — you'll paste these into `.env` later.

---

## Values to collect (fill in as you discover them)

```
# Camera
CAMERA_IP=
CAMERA_USER=
CAMERA_PASSWORD=

# QNAP
QNAP_IP=192.168.1.250
QNAP_USER=
QNAP_PASSWORD=

# Telegram
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_ALLOWED_USERS=

# Weather (optional)
OPENWEATHER_API_KEY=

# Location
LOCATION_NAME=
LOCATION_LAT=
LOCATION_LON=
LOCATION_TIMEZONE=America/Toronto   # or your IANA tz
```

---

## 1. QNAP — *required only if you want DVR + persistent NAS storage; stack runs fine without it*

- [ ] Finish QTS Smart Install — admin password, hostname, timezone
- [ ] Create Storage Pool + Volume from your disks
- [ ] Create user `visionlabs` (or your choice) — record password above
- [ ] Create one shared folder named **`vision-labs`** (exact, lowercase)
- [ ] Inside it, create 7 subfolders: `snapshots/`, `recordings/`, `events/`, `telegram/`, `generations/`, `videos/`, `clips/`
- [ ] Grant `visionlabs` user **RW** on `vision-labs`
- [ ] Enable **SMB 3.0** (Control Panel → Network & File Services → SMB). Disable SMB 1.
- [ ] Set the QNAP a static IP — either at the UDM (DHCP reservation) or in QTS (Network → choose interface → manual IP). Aim to keep it at `192.168.1.250` since that's already in `.env.example`.
- [ ] Sanity test from PowerShell on your PC:
  ```
  net use Z: \\192.168.1.250\vision-labs /user:<qnap_user> <qnap_password>
  dir Z:
  net use Z: /delete
  ```
  You should see all 7 subfolders.

---

## 2. UDM / Network admin

- [ ] In the UDM dashboard, set a **DHCP reservation** for the QNAP MAC → `192.168.1.250`.
- [ ] Find the **Reolink camera's current IP** in the UDM client list. Set a DHCP reservation for it too — write the IP into `CAMERA_IP` above.
- [ ] Confirm the camera and your PC are on the **same subnet** (e.g., both `192.168.1.x` or both `192.168.2.x`). If they're on different subnets, note that — we'll need to discuss routing.
- [ ] If the camera is connected through a WiFi antenna/extender, find out whether that device is in **bridge mode** (camera shows up directly in UDM client list) or **NAT mode** (camera is hidden behind it). Bridge mode is what you want.

---

## 3. Reolink camera

- [ ] Log in to the camera's web UI at its IP. Note `CAMERA_USER` and `CAMERA_PASSWORD` above.
- [ ] **Network → Advanced → Port** — confirm RTSP is enabled and the port is **554** (default). If different, tell me and we'll update the URLs in compose.
- [ ] **Recording → Encode** — for the **sub-stream**: H.264, 640×480, 15 fps, ~512 kbps. (Lower bandwidth than HD; this is what detection runs on.)
- [ ] **Recording → Encode** — for the **main stream**: H.264 (not H.265 — ffmpeg `copy` mode handles H.264 cleanest), 2K or 4K, 15 fps, ~6 Mbps. Higher bitrate is fine if WiFi handles it.
- [ ] Test the sub-stream URL works from your PC. Install ffmpeg first (`winget install ffmpeg` in PowerShell, or `sudo apt install ffmpeg` in WSL), then:
  ```
  ffprobe -rtsp_transport tcp rtsp://<user>:<pass>@<camera_ip>:554/h264Preview_01_sub
  ```
  You should see codec/resolution info, not an error.

---

## 4. Telegram bot (optional but recommended)

If you want notifications + the bot commands:

- [ ] In Telegram, message **@BotFather** → `/newbot` → pick a name + username → it gives you a token. Paste into `TELEGRAM_BOT_TOKEN`.
- [ ] Message **@userinfobot** with `/start` — it returns your numeric user ID. Paste into both `TELEGRAM_CHAT_ID` and `TELEGRAM_ALLOWED_USERS`.
- [ ] (Optional) Add other people's user IDs to `TELEGRAM_ALLOWED_USERS` (comma-separated) to grant them bot access.

---

## 5. OpenWeather (optional — for the conditions panel)

- [ ] Sign up free at https://openweathermap.org/api → grab an API key → paste into `OPENWEATHER_API_KEY`. Skip this whole step if you don't care about the weather widget.

---

## 6. Location (for sunrise/sunset + zone time-rules)

- [ ] Pick `LOCATION_NAME` (free-form, e.g., "Toronto").
- [ ] Look up your latitude/longitude — Google Maps, right-click your house → click the coords to copy them. Paste into `LOCATION_LAT` and `LOCATION_LON`.
- [ ] `LOCATION_TIMEZONE` — IANA name. `America/Toronto`, `America/Vancouver`, `Europe/London`, etc.

---

## 7. Get off Docker Desktop, onto Docker Engine inside WSL2 — **DONE on current host**

This is the chunkiest manual piece. Plan ~30 minutes. Already complete on this machine; keep these steps for reference if you ever rebuild.

- [ ] Open PowerShell **as Admin** → confirm WSL2 is your default: `wsl --set-default-version 2`
- [ ] If you don't have Ubuntu yet: `wsl --install -d Ubuntu-24.04`
- [ ] Make sure your NVIDIA Windows driver is **R555 or newer** (`nvidia-smi` from Windows shows the version). Older drivers don't expose Blackwell to WSL cleanly. Update from https://www.nvidia.com/Download/index.aspx if needed.
- [ ] **Uninstall Docker Desktop** from Windows (Apps → Docker Desktop → Uninstall). Reboot Windows.
- [ ] In WSL, install Docker Engine:
  ```
  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker $USER
  ```
  Then close + reopen WSL so the group membership takes.
- [ ] Install NVIDIA Container Toolkit in WSL:
  ```
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
  sudo apt update && sudo apt install -y nvidia-container-toolkit
  sudo nvidia-ctk runtime configure --runtime=docker
  sudo service docker restart
  ```
- [ ] Verify the whole stack works inside WSL:
  ```
  docker version
  docker compose version
  docker run --rm --gpus all nvidia/cuda:12.8.0-base-ubuntu24.04 nvidia-smi
  ```
  The last command should print **both** GPUs (3090 and 5070 Ti). **If it doesn't, stop and tell me — that's the foundation everything else stands on.**

---

## 8. Move the project off `/mnt/c` into WSL ext4 — **DONE on current host**

- [ ] In WSL: `mkdir -p ~/projects && mv /mnt/c/Users/adhaliwal/python-projects/vision-labs-v1-main ~/projects/vision-labs`
- [ ] Confirm: `cd ~/projects/vision-labs && df -T .` — last column should say `ext4`, not `9p`.
- [ ] `git init` if not already a repo (this folder isn't currently a git repo) — so we can track the changes we're about to make:
  ```
  git init
  git add .
  git -c user.email=you@local -c user.name=you commit -m "import"
  ```

---

## 8a. Choose a hardware tier (optional)

The defaults in `.env.example` are tuned for a **single 8-12 GB GPU** running 1-3 cameras with a 14B chat model. If your GPU is bigger, smaller, or you have two cards, append the matching preset to `.env`:

| Preset | Target hardware | What changes |
|---|---|---|
| `tiers/small.env` | 6 GB single GPU (1660 Ti, 3050) | Nano YOLO models, AI chat disabled (saves 5-9 GB VRAM), vision LLM off, target_fps=5 |
| `tiers/mid.env`   | 8-12 GB single GPU (3060, 4060) | 's' YOLO models, Qwen 3 **7B** chat (~5 GB), vision LLM off |
| `tiers/full.env`  | 16+ GB single or dual-GPU | 's' YOLO, Qwen 3 **14B** chat, vision LLM on |

```bash
cat tiers/full.env >> .env
# Then edit .env to fix any duplicate keys — last value wins per line, so
# the appended block overrides earlier defaults.
```

### Per-GPU control (manual override)

Two env vars decide which card each workload lands on. Both default to `0` (single-GPU). On this dual-GPU workstation, the .env sets `CHAT_GPU=1`.

| Variable | Default | Effect |
|---|---|---|
| `DETECTOR_GPU` | `0` | Pose, vehicle, and face-recognizer all use this GPU |
| `CHAT_GPU` | `0` | Ollama (chat + vision LLM) uses this GPU. Set to `1` to dedicate a second card |

Indexes match `nvidia-smi -L` output thanks to `CUDA_DEVICE_ORDER=PCI_BUS_ID` (set automatically inside every GPU container). On WSL2, both `NVIDIA_VISIBLE_DEVICES` and `CUDA_VISIBLE_DEVICES` are required to isolate a card — the compose file sets both for you.

To swap a 2-GPU rig from "ollama on card 1" to "everything on card 0":
```bash
sed -i 's/^CHAT_GPU=1/CHAT_GPU=0/' .env
docker compose up -d   # only ollama gets recreated, ~10s downtime
```

---

## 9. Diagnostics to paste back when each section is done

Once you've finished sections 1–8, paste the output of each block to me and I'll move us into the code-side phases.

**From PowerShell (Windows host):**
```
ipconfig
ping <camera_ip>
ping 192.168.1.250
arp -a
```

**From WSL:**
```
docker version
docker info | grep -i runtime
nvidia-smi
docker run --rm --gpus all nvidia/cuda:12.8.0-base-ubuntu24.04 nvidia-smi
ls ~/projects/vision-labs
df -T ~/projects/vision-labs
```

---

## What I'll handle once your manual stuff is done

For reference (not your problem to do). Most of these are now complete:

- ~~Bumping all four GPU service Dockerfiles to CUDA 12.8.~~ — **done**
- ~~Pinning services to specific GPUs in `docker-compose.yml` (3090 vs 5070 Ti split).~~ — **done**
- ~~Wiring `QNAP_ENABLED` flag through compose~~ — **done via override file pattern (`docker-compose.qnap.yml`) + `--profile nas`**
- ~~Adding Portainer service to compose.~~ — **done (https://localhost:9443)**
- ~~Snapshot retention prune~~ — **done (`SNAPSHOT_RETENTION_DAYS`, default 4 days)**
- ~~Telegram offset persistence~~ — **done (`telegram:last_offset` Redis key)**
- ~~Identity_state cleared on empty scene~~ — **done**
- ~~`target_fps` hot-reload~~ — **done (ingester + WS render rate)**
- ~~WebSocket authentication~~ — **done (validates `vl_session` cookie, closes 4401 if invalid)**
- ~~Face-recognizer port not host-exposed~~ — **done (`expose` instead of `ports`)**
- ~~httpx logging silenced so Telegram bot token doesn't leak~~ — **done**
- ~~Forced admin password rotation on first login~~ — **done**

Outstanding items live in `PHASES.md` Phase 5 (vehicle stationarity reset, dead-zone normalized-coords mismatch, bcrypt/argon2 password hashing, graceful poller shutdown, recorder error events, etc.). Earlier "sticky-identity cache" and hardcoded model names are done — see the matching `[x]` items in PHASES.md.
