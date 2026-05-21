"""
services/dashboard/routes/setup.py — first-run setup wizard backend.

PURPOSE:
    Tracks whether the dashboard has been through its initial setup flow.
    A new install starts with no /data/setup-state/setup.json file; once
    the wizard completes, it writes one with a timestamp + step summary.
    The setup-gate middleware (see server.py) consults this to decide
    whether to redirect new visitors to /setup.html.

ENDPOINTS:
    GET  /api/setup/status            — is setup complete? unauthenticated
    POST /api/setup/detect-hardware   — orchestrator-spawned GPU probe; auth
    POST /api/setup/complete          — mark setup done; auth

DATA MODEL — /data/setup-state/setup.json:
    {
        "version": 1,
        "completed_at": "2026-05-18T03:30:00Z",
        "steps": ["hardware_detected", "camera_added", "telegram_skipped"],
        "hardware": {"gpus": [{"index": 0, "name": "RTX 3060", "vram_mb": 12288}]}
    }

EXISTING-INSTALL DETECTION:
    On dashboard startup (server.py), if setup.json is missing BUT
    cameras:registry has ≥1 camera AND a non-default admin exists, we
    write setup.json automatically with steps=["preexisting-install"].
    This avoids force-marching existing users through the wizard after
    a software update.

ORCHESTRATOR INTEGRATION:
    The hardware-probe endpoint can't run nvidia-smi itself (dashboard
    has no Docker socket by design — Phase 7b decision). It publishes
    to Redis pub/sub channel `setup:probe-request` and awaits a result
    on the `setup:probe-result` stream. The orchestrator listens for
    these requests and spawns a one-shot `nvidia/cuda:base nvidia-smi`
    container with --gpus all. ~200 MB image pull on first wizard run.
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import redis
from contracts.redis_client import make_redis_client
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse


logger = logging.getLogger("dashboard.setup")
router = APIRouter(prefix="/api/setup", tags=["setup"])

# /data is the existing Docker-managed volume; we add a setup/ subdir so the
# state file doesn't collide with auth.db / ai.db / faces.db.
SETUP_STATE_PATH = Path(os.getenv("SETUP_STATE_PATH", "/data/setup-state/setup.json"))

PROBE_REQUEST_CHANNEL = "setup:probe-request"
PROBE_RESULT_STREAM = "setup:probe-result"
# Must exceed orchestrator's PROBE_TIMEOUT (120s). On a fresh install the
# orchestrator first pulls the nvidia/cuda base image (~200 MB), then runs
# nvidia-smi — both happen within 120s, but the dashboard previously gave
# up at 60s and returned a false-negative "probe timed out" while the
# orchestrator was still working in the background.
PROBE_TIMEOUT_SECONDS = 150


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _redis_client():
    return make_redis_client(decode_responses=True)


def _load_state() -> dict | None:
    """Return the setup.json contents, or None if setup hasn't completed."""
    try:
        if not SETUP_STATE_PATH.exists():
            return None
        with SETUP_STATE_PATH.open() as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Couldn't read setup state at {SETUP_STATE_PATH}: {e}")
        return None


def _write_state(state: dict) -> None:
    """Atomically write setup.json (write to .tmp, then rename).

    Falls back to a truncate-and-write when the rename fails with EBUSY —
    this happens when SETUP_STATE_PATH itself is a bind-mount target
    (someone mapped `-v ./setup.json:/data/setup-state/setup.json` for
    inspection). Same workaround pattern as env_writer.
    """
    SETUP_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, indent=2)
    tmp = SETUP_STATE_PATH.with_suffix(".tmp")
    try:
        with tmp.open("w") as f:
            f.write(payload)
        tmp.replace(SETUP_STATE_PATH)
    except OSError as e:
        if getattr(e, "errno", None) == 16:  # EBUSY → bind-mount target
            logger.warning(
                "setup.json appears bind-mounted (EBUSY); using truncate fallback"
            )
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            with SETUP_STATE_PATH.open("w") as f:
                f.write(payload)
        else:
            raise


def is_setup_complete() -> bool:
    """Module-level helper for the setup-gate middleware in server.py."""
    return _load_state() is not None


def auto_mark_complete_if_preexisting() -> bool:
    """
    Called from server.py startup. If setup.json is missing but the install
    is clearly not new (camera registry populated, admin password rotated),
    write setup.json so we don't force-march the user through the wizard.

    Returns True if we just wrote setup.json as a result of this check.
    """
    if _load_state() is not None:
        return False

    # Redis may be slow to come up alongside the dashboard on first boot
    # after `docker compose up`. Retry a few times before treating an
    # unreadable registry as "fresh install" — otherwise a backup-restore
    # bootup where Redis lags by a few seconds would force the user through
    # the wizard with cameras already registered.
    camera_count = 0
    last_error = None
    for attempt in range(5):
        try:
            r = _redis_client()
            camera_count = r.hlen("cameras:registry")
            last_error = None
            break
        except Exception as e:
            last_error = e
            time.sleep(2)
    if last_error is not None:
        logger.debug(f"Pre-existing-install check: registry read failed: {last_error}")

    # The dashboard already has a forced-rotation flow for admin/admin, so by
    # the time anyone has a working stack with at least one camera, they've
    # already changed the admin password. One camera in the registry is the
    # simplest "not a fresh install" signal.
    if camera_count >= 1:
        state = {
            "version": 1,
            "completed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "steps": ["preexisting-install"],
            "hardware": {},
        }
        try:
            _write_state(state)
            logger.info(
                f"Pre-existing install detected ({camera_count} cameras in registry); "
                f"marking setup complete to skip the first-run wizard"
            )
            return True
        except OSError as e:
            logger.warning(f"Couldn't auto-mark setup complete: {e}")

    return False


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.get("/timezones")
async def list_timezones():
    """Return the full IANA timezone list, sorted, grouped by region prefix.

    Used by the setup wizard's location step. Region grouping keeps the
    dropdown navigable (~600 entries otherwise) — UI puts an <optgroup>
    per region (Africa, America, Asia, ...).
    """
    from zoneinfo import available_timezones
    zones = sorted(available_timezones())
    grouped: dict[str, list[str]] = {}
    for z in zones:
        # "America/Toronto" -> region="America"; "UTC" -> region="Other"
        region = z.split("/", 1)[0] if "/" in z else "Other"
        grouped.setdefault(region, []).append(z)
    return {
        "regions": sorted(grouped.keys()),
        "zones": grouped,
        "total": len(zones),
    }


@router.get("/status")
async def get_status():
    """
    Reports whether setup has completed. Reached through the global
    auth middleware, which means callers need a valid session cookie —
    not a problem for the actual wizard (it runs post-login), but worth
    knowing if you're testing this endpoint directly.
    """
    state = _load_state()
    if state is None:
        return {"completed": False}
    return {
        "completed": True,
        "completed_at": state.get("completed_at"),
        "steps": state.get("steps", []),
    }


@router.post("/detect-hardware")
async def detect_hardware(request: Request):
    """
    Asks the orchestrator to run an nvidia-smi probe via a one-shot CUDA
    container. The dashboard does not have the Docker socket by design;
    the orchestrator does.

    Returns: {"gpus": [{"index": 0, "name": "RTX 3060", "vram_mb": 12288}, ...]}
    Or: {"gpus": [], "error": "no GPU detected / probe timed out"}
    """
    r = _redis_client()
    request_id = f"probe-{int(time.time() * 1000)}"

    # Mark our cursor so we only read NEW probe results, not stale ones.
    cursor = "$"

    try:
        r.publish(PROBE_REQUEST_CHANNEL, json.dumps({"request_id": request_id}))
        logger.info(f"Hardware probe requested (request_id={request_id})")
    except redis.ConnectionError as e:
        logger.error(f"Couldn't publish probe request: {e}")
        return JSONResponse(status_code=503, content={"gpus": [], "error": "redis unreachable"})

    # Wait for the orchestrator to push a result onto the stream. We use
    # XREAD blocking with our cursor; loop until timeout or matching id.
    start = time.time()
    while time.time() - start < PROBE_TIMEOUT_SECONDS:
        remaining_ms = max(1, int((PROBE_TIMEOUT_SECONDS - (time.time() - start)) * 1000))
        try:
            messages = r.xread({PROBE_RESULT_STREAM: cursor}, block=min(5000, remaining_ms), count=10)
        except redis.ConnectionError:
            await asyncio.sleep(1)
            continue

        if not messages:
            continue

        for _stream, entries in messages:
            for entry_id, fields in entries:
                cursor = entry_id  # advance cursor
                if fields.get("request_id") != request_id:
                    continue

                # Found our reply. Parse and return.
                try:
                    payload = json.loads(fields.get("payload", "{}"))
                    return payload
                except json.JSONDecodeError:
                    return {"gpus": [], "error": "orchestrator returned malformed payload"}

    # Timed out waiting for orchestrator.
    return {"gpus": [], "error": f"orchestrator probe timed out after {PROBE_TIMEOUT_SECONDS}s"}


@router.post("/discover-cameras")
async def discover_cameras_in_setup(request: Request):
    """Setup-wizard wrapper that reuses the same scanner the cameras tab uses.

    The wizard calls this from /setup.html step 3 to populate the "Scan my
    network" picker. The actual scanning logic lives in routes/cameras.py
    so the cameras tab can reuse the exact same response shape.
    """
    from routes.cameras import discover_cameras
    return await discover_cameras(request)


# ---------------------------------------------------------------------------
# Telegram bot setup — wizard step 4.5
# ---------------------------------------------------------------------------
# Two-step flow that makes the painful "find your chat id" part automatic:
#   1. User pastes the bot token from @BotFather.
#      POST /api/setup/telegram/validate-token  → calls getMe, returns bot info.
#   2. User sends /start to the bot from their phone.
#      POST /api/setup/telegram/discover-chat-id → polls getUpdates for
#      ~30 s, extracts the chat_id of the first incoming message.
#   3. POST /api/setup/telegram/save writes token + chat_id + allowed users
#      via the normal apply-config path, then sends a confirmation message.
#
# Why split into endpoints (instead of one big call): the chat-id discovery
# needs a long poll the user can cancel ("skip Telegram"). And token validation
# can fail fast without committing anything.

import httpx as _httpx


@router.post("/telegram/validate-token")
async def telegram_validate_token(request: Request):
    """Verify a bot token by calling Telegram's getMe. Returns the bot's
    username on success so the wizard can show "send /start to @yourbot"."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
    token = (body.get("token") or "").strip()
    # Format sanity — bot tokens are <int>:<29-char-base64ish>.
    if not re.fullmatch(r"\d{6,}:[A-Za-z0-9_-]{20,}", token):
        return JSONResponse(
            {"ok": False, "error": "Token doesn't look right — should be <numbers>:<letters/numbers>"},
            status_code=400,
        )
    try:
        async with _httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.telegram.org/bot{token}/getMe", timeout=10
            )
    except _httpx.HTTPError as e:
        logger.warning(f"Telegram API request failed: {e}")
        return JSONResponse(
            {"ok": False, "error": "Couldn't reach Telegram — check network connectivity"},
            status_code=502,
        )
    data = resp.json() if resp.status_code == 200 else {}
    if not data.get("ok"):
        return JSONResponse(
            {"ok": False, "error": data.get("description", "Token rejected by Telegram")},
            status_code=400,
        )
    info = data.get("result", {})
    return {
        "ok": True,
        "username": info.get("username", ""),
        "first_name": info.get("first_name", ""),
        "id": info.get("id"),
    }


@router.post("/telegram/discover-chat-id")
async def telegram_discover_chat_id(request: Request):
    """Poll getUpdates for up to ~30 seconds, return the first chat_id we see.

    The wizard tells the user to send /start to the bot, then calls this.
    The first incoming message gives us the user's chat_id — written to .env
    in the save step so notifications can target them.

    NOTE: We don't acknowledge the updates (no offset bump). The Telegram
    bot poller in routes/bot_commands will pick them up cleanly once the
    dashboard restarts with the saved token.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
    token = (body.get("token") or "").strip()
    if not token:
        return JSONResponse({"ok": False, "error": "Missing token"}, status_code=400)

    deadline = time.time() + 35.0
    async with _httpx.AsyncClient() as client:
        while time.time() < deadline:
            try:
                resp = await client.get(
                    f"https://api.telegram.org/bot{token}/getUpdates",
                    params={"timeout": 5, "allowed_updates": '["message"]'},
                    timeout=10,
                )
            except _httpx.HTTPError:
                await asyncio.sleep(2)
                continue
            if resp.status_code != 200:
                await asyncio.sleep(2)
                continue
            data = resp.json()
            for update in data.get("result", []):
                msg = update.get("message") or {}
                chat = msg.get("chat") or {}
                from_user = msg.get("from") or {}
                if chat.get("id"):
                    return {
                        "ok": True,
                        "chat_id": chat["id"],
                        "user_id": from_user.get("id"),
                        "first_name": from_user.get("first_name", ""),
                        "username": from_user.get("username", ""),
                    }
            await asyncio.sleep(2)

    return {
        "ok": False,
        "error": (
            "No message received after 30 seconds. Make sure you sent a "
            "message (any message) to the bot from the Telegram app, then "
            "click 'Find me again'."
        ),
    }


@router.post("/telegram/save")
async def telegram_save(request: Request):
    """Persist token + chat_id + allow-list to .env via apply-config and
    send a confirmation message so the user immediately sees it works."""
    from helpers.env_writer import update_env
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
    token = (body.get("token") or "").strip()
    chat_id = (body.get("chat_id") or "").strip() or str(body.get("chat_id") or "")
    user_id = str(body.get("user_id") or chat_id)
    if not token or not chat_id:
        return JSONResponse(
            {"ok": False, "error": "Need both token and chat_id"}, status_code=400
        )

    updates = {
        "TELEGRAM_BOT_TOKEN": token,
        "TELEGRAM_CHAT_ID": chat_id,
        "TELEGRAM_ALLOWED_USERS": user_id,
    }
    result = update_env(updates)
    if not result["ok"]:
        logger.error(f"update_env failed (telegram/save): {result['error']}")
        return JSONResponse(
            {"ok": False, "error": "Failed to write configuration — see dashboard logs for details"}, status_code=500
        )

    # Best-effort confirmation message. We send NOW with the freshly-supplied
    # token (the dashboard's existing notifications module won't pick up the
    # new env until restart, so we use httpx directly here).
    try:
        async with _httpx.AsyncClient() as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": "✅ Vision Labs is connected.\n\nYou'll start seeing alerts here whenever the system detects a person, vehicle, or face. Send /help to the bot for available commands.",
                    "parse_mode": "HTML",
                },
                timeout=10,
            )
    except Exception as e:
        logger.warning(f"Telegram confirmation message failed: {e}")

    # Tell the orchestrator to restart the dashboard so the long-running
    # bot-command poller picks up the new token.
    try:
        r = _redis_client()
        r.publish("config:apply", json.dumps({
            "request_id": f"tg-{int(time.time() * 1000)}",
            "services": ["dashboard"],
            "keys_changed": list(updates.keys()),
        }))
    except redis.ConnectionError as e:
        logger.warning(f"Couldn't notify orchestrator: {e}")

    return {"ok": True, "written": result["written"]}


@router.post("/apply-config")
async def apply_config(request: Request):
    """Persist the user's hardware-tier / GPU-mode / model choices to .env
    and signal the orchestrator to restart the affected services.

    Body (every field is optional — only present fields get written):
      {
        "detector_gpu": "0" | "1",
        "chat_gpu":     "0" | "1",
        "chat_model":   "qwen3:14b" | "qwen3:7b" | "qwen3:3b" | "" (disable),
        "vision_model": "minicpm-v" | "" (disable),
        "pose_model":   "/models/yolov8s-pose.pt" | "/models/yolov8n-pose.pt",
        "vehicle_model":"/models/yolov8s.pt" | "/models/yolov8n.pt",
        "target_fps":   "5" | "10" | "15"
      }

    Returns:
      { ok: bool, written: [..keys..], affected_services: [..], error: ... }

    Side effects:
      1. /app/.env is updated in place (bind-mounted to host's .env)
      2. A message is published on Redis pub/sub channel "config:apply"
         with the set of services that need to be recreated to pick up
         the new env. The orchestrator (which has the Docker socket)
         handles the actual `docker compose up -d --force-recreate`.
    """
    from helpers.env_writer import update_env, ALLOWED_KEYS

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

    # Map body's lowercase keys -> .env UPPER_SNAKE_CASE
    key_map = {
        "detector_gpu":  "DETECTOR_GPU",
        "chat_gpu":      "CHAT_GPU",
        "chat_model":    "CHAT_MODEL",
        "vision_model":  "VISION_MODEL",
        "pose_model":    "POSE_MODEL",
        "vehicle_model": "VEHICLE_MODEL",
        "target_fps":    "TARGET_FPS",
    }
    updates = {key_map[k]: str(body[k]) for k in body if k in key_map}
    # Also accept the canonical UPPER_SNAKE_CASE form directly (used by the
    # location wizard step which has no lowercase aliases worth maintaining).
    for k in body:
        if k in ALLOWED_KEYS and k not in updates:
            updates[k] = str(body[k])

    if not updates:
        return {"ok": True, "written": [], "affected_services": [], "error": None}

    # Validate
    if "DETECTOR_GPU" in updates and updates["DETECTOR_GPU"] not in ("0", "1", "2", "3"):
        return JSONResponse({"ok": False, "error": "detector_gpu must be 0/1/2/3"}, status_code=400)
    if "CHAT_GPU" in updates and updates["CHAT_GPU"] not in ("0", "1", "2", "3"):
        return JSONResponse({"ok": False, "error": "chat_gpu must be 0/1/2/3"}, status_code=400)
    # Restrict model paths so a malicious / fat-fingered request can't
    # write `POSE_MODEL=/etc/passwd` into .env. The detector would just
    # fail to load, but it's cleaner to reject at the API.
    _MODEL_PATH_RE = re.compile(r"^/models/[A-Za-z0-9_./-]+$")
    if "POSE_MODEL" in updates and not _MODEL_PATH_RE.match(updates["POSE_MODEL"]):
        return JSONResponse(
            {"ok": False, "error": "pose_model must look like /models/<name>"},
            status_code=400,
        )
    if "VEHICLE_MODEL" in updates and not _MODEL_PATH_RE.match(updates["VEHICLE_MODEL"]):
        return JSONResponse(
            {"ok": False, "error": "vehicle_model must look like /models/<name>"},
            status_code=400,
        )
    if "LOCATION_TIMEZONE" in updates:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(updates["LOCATION_TIMEZONE"])
        except ZoneInfoNotFoundError:
            return JSONResponse(
                {"ok": False,
                 "error": f"invalid timezone {updates['LOCATION_TIMEZONE']!r} — must be a valid IANA name"},
                status_code=400,
            )
    for ret_key, lo, hi in (
        ("RETENTION_DAYS", 1, 365),
        ("SNAPSHOT_RETENTION_DAYS", 0, 90),
        ("CLIP_RETENTION_DAYS", 1, 30),
    ):
        if ret_key in updates:
            try:
                v = int(updates[ret_key])
            except (TypeError, ValueError):
                return JSONResponse(
                    {"ok": False, "error": f"{ret_key} must be an integer"},
                    status_code=400,
                )
            if not (lo <= v <= hi):
                return JSONResponse(
                    {"ok": False, "error": f"{ret_key} must be between {lo} and {hi}"},
                    status_code=400,
                )
            updates[ret_key] = str(v)  # normalize

    result = update_env(updates)
    if not result["ok"]:
        logger.error(f"update_env failed (apply-config): {result['error']}")
        return JSONResponse({"ok": False, "error": "Failed to write configuration — see dashboard logs for details"}, status_code=500)

    # Figure out which services need to restart so the orchestrator knows
    # what to recreate. Detectors picking up DETECTOR_GPU + POSE/VEHICLE_MODEL
    # changes; ollama for CHAT_GPU; dashboard for CHAT_MODEL/VISION_MODEL
    # (env-var reads happen at process startup).
    affected: set[str] = set()
    if any(k in updates for k in ("DETECTOR_GPU", "POSE_MODEL", "VEHICLE_MODEL", "TARGET_FPS")):
        affected.update(["pose-detector", "vehicle-detector", "face-recognizer", "camera-ingester"])
    if "CHAT_GPU" in updates:
        affected.add("ollama")
    if any(k in updates for k in ("CHAT_MODEL", "VISION_MODEL")):
        affected.add("dashboard")
    if any(k in updates for k in (
        "LOCATION_TIMEZONE", "LOCATION_NAME", "LOCATION_REGION",
        "LOCATION_LAT", "LOCATION_LON",
    )):
        # Every service that imports `contracts.tz.TZ_LOCAL` reads the env
        # var at process startup and caches the resolved ZoneInfo at module
        # scope — so a restart is the only way to pick up a new timezone.
        # Restarting just `dashboard` left the recorder writing segment
        # filenames in the wrong day folder + the tracker stamping events
        # with the wrong local time. Per-cam services (tracker, recorder,
        # camera-ingester) get expanded against the registry by the
        # orchestrator's apply_config — see services/orchestrator/orchestrator.py.
        affected.add("dashboard")
        affected.add("tracker")
        affected.add("recorder")
        affected.add("camera-ingester")
        # Grafana reads TZ + GF_DATE_FORMATS_DEFAULT_TIMEZONE from .env via
        # compose interpolation — without a recreate, dashboard panels keep
        # rendering timestamps in the old timezone.
        affected.add("grafana")
    # NOTE: SNAPSHOT_RETENTION_DAYS + CLIP_RETENTION_DAYS DO NOT need a
    # dashboard restart — the retention poller re-reads env each cycle (hourly).
    if "RETENTION_DAYS" in updates:
        # Recorder still reads RETENTION_DAYS at startup, so it does need a restart.
        affected.add("recorder")

    # Tell the orchestrator
    try:
        r = _redis_client()
        r.publish("config:apply", json.dumps({
            "request_id": f"cfg-{int(time.time() * 1000)}",
            "services": sorted(affected),
            "keys_changed": result["written"],
        }))
    except redis.ConnectionError as e:
        logger.warning(f"Couldn't notify orchestrator about config change: {e}")
        # The write happened; just inform the user that auto-restart won't fire
        return {
            "ok": True,
            "written": result["written"],
            "affected_services": sorted(affected),
            "error": "config written but orchestrator notification failed — restart affected services manually",
        }

    return {
        "ok": True,
        "written": result["written"],
        "ignored": result["ignored"],
        "affected_services": sorted(affected),
        "error": None,
    }


@router.post("/complete")
async def complete_setup(request: Request):
    """
    Writes /data/setup-state/setup.json with a summary of what was done.
    After this, the setup-gate middleware stops redirecting.
    """
    body = await request.json()
    steps = body.get("steps", [])
    hardware = body.get("hardware", {})

    state = {
        "version": 1,
        "completed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "steps": steps,
        "hardware": hardware,
    }

    try:
        _write_state(state)
    except OSError as e:
        logger.error(f"Couldn't write setup state: {e}")
        return JSONResponse(status_code=500, content={"error": "Failed to write setup state — see dashboard logs for details"})

    logger.info(f"Setup completed: {steps}")
    return {"ok": True, "completed_at": state["completed_at"]}
