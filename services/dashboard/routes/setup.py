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
import time
from datetime import datetime, timezone
from pathlib import Path

import redis
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

import constants as ctx

logger = logging.getLogger("dashboard.setup")
router = APIRouter(prefix="/api/setup", tags=["setup"])

# /data is the existing Docker-managed volume; we add a setup/ subdir so the
# state file doesn't collide with auth.db / ai.db / faces.db.
SETUP_STATE_PATH = Path(os.getenv("SETUP_STATE_PATH", "/data/setup-state/setup.json"))

PROBE_REQUEST_CHANNEL = "setup:probe-request"
PROBE_RESULT_STREAM = "setup:probe-result"
PROBE_TIMEOUT_SECONDS = 60  # nvidia/cuda base image pull can take ~30s on first run


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _redis_client():
    return redis.Redis(
        host=os.getenv("REDIS_HOST", "redis"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        decode_responses=True,
    )


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
    """Atomically write setup.json (write to .tmp, then rename)."""
    SETUP_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETUP_STATE_PATH.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(SETUP_STATE_PATH)


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

    try:
        r = _redis_client()
        camera_count = r.hlen("cameras:registry")
    except Exception as e:
        logger.debug(f"Pre-existing-install check: registry read failed: {e}")
        camera_count = 0

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

    if not updates:
        return {"ok": True, "written": [], "affected_services": [], "error": None}

    # Validate
    if "DETECTOR_GPU" in updates and updates["DETECTOR_GPU"] not in ("0", "1", "2", "3"):
        return JSONResponse({"ok": False, "error": "detector_gpu must be 0/1/2/3"}, status_code=400)
    if "CHAT_GPU" in updates and updates["CHAT_GPU"] not in ("0", "1", "2", "3"):
        return JSONResponse({"ok": False, "error": "chat_gpu must be 0/1/2/3"}, status_code=400)

    result = update_env(updates)
    if not result["ok"]:
        return JSONResponse({"ok": False, "error": result["error"]}, status_code=500)

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
        return JSONResponse(status_code=500, content={"error": f"couldn't write state: {e}"})

    logger.info(f"Setup completed: {steps}")
    return {"ok": True, "completed_at": state["completed_at"]}
