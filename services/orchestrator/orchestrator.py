"""
services/orchestrator/orchestrator.py — Compose-profile reconciler.

PURPOSE:
    Single-purpose worker that watches the camera registry and ensures
    the corresponding docker-compose profiles are running. The dashboard
    publishes events when a camera is created, updated (enable/disable),
    or deleted; this service reacts within seconds. A 10-second reconcile
    loop runs as a safety net in case events are missed (e.g. dashboard
    was down when a registry change happened).

DESIGN NOTES:
    - No HTTP server. No incoming surface. Pure Redis-driven.
    - Only operates on profiles listed in ALLOWED_PROFILES.
      Anything outside that list is logged and ignored.
    - All actions land in `orchestrator:audit` stream so the dashboard
      can show live status.
    - Uses docker CLI + compose CLI (already in the docker:24-cli base)
      via subprocess rather than the python `docker` library, because
      compose semantics (profiles, depends_on, healthchecks, GPU
      device_ids) are non-trivial to replicate by hand.

ENV VARS (all optional, sane defaults):
    REDIS_HOST            — default "redis"
    REDIS_PORT            — default 6379
    HOST_PROJECT_DIR      — host filesystem path of the project root.
                            Compose --project-directory uses this so
                            build contexts resolve correctly when the
                            CLI is talking to the host Docker daemon.
    COMPOSE_PROJECT_NAME  — default "vision-labs" (matches the directory
                            name `docker compose` derives by default)
    ALLOWED_PROFILES      — comma-separated list of profile names this
                            service is permitted to up/down.
                            Default "cam2,cam3,cam4,cam5".
    RECONCILE_INTERVAL    — seconds between safety-net reconcile passes
                            (default 10).

REDIS KEYS / CHANNELS:
    cameras:registry      — read-only source of truth (camera entries)
    cameras:events        — pub/sub channel; dashboard publishes here
                            when a camera is added / updated / removed.
                            Any message triggers a reconcile.
    orchestrator:audit    — append-only stream of every action this
                            service takes. Capped at 500 entries.
"""

import json
import logging
import os
import subprocess
import sys
import time
import threading

import redis


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

# Host filesystem path of the project root. Defaulted to /workspace so a dev
# bind mount at that path works; in production set HOST_PROJECT_DIR=${PWD}
# so compose can resolve build contexts on the actual host filesystem.
HOST_PROJECT_DIR = os.getenv("HOST_PROJECT_DIR", "/workspace")
COMPOSE_PROJECT_NAME = os.getenv("COMPOSE_PROJECT_NAME", "vision-labs")

# Where the compose file lives AS THIS CONTAINER SEES IT (bind-mounted).
CONTAINER_COMPOSE_FILE = "/workspace/docker-compose.yml"

# Profiles we are willing to up/down. Strict allowlist to bound blast radius.
ALLOWED_PROFILES = {
    p.strip()
    for p in os.getenv("ALLOWED_PROFILES", "cam1,cam2,cam3,cam4,cam5").split(",")
    if p.strip()
}

RECONCILE_INTERVAL = int(os.getenv("RECONCILE_INTERVAL", "10"))

# Redis keys / channels
REGISTRY_KEY = "cameras:registry"
EVENTS_CHANNEL = "cameras:events"
AUDIT_STREAM = "orchestrator:audit"
AUDIT_MAXLEN = 500

# Setup-wizard hardware probe (Phase D). Dashboard publishes a request,
# we spawn a one-shot nvidia-smi container and stream the result back.
PROBE_REQUEST_CHANNEL = "setup:probe-request"
PROBE_RESULT_STREAM = "setup:probe-result"
PROBE_RESULT_MAXLEN = 50  # tiny — probe payloads are small + only consumed once
PROBE_IMAGE = os.getenv("HW_PROBE_IMAGE", "nvidia/cuda:12.4.0-base-ubuntu22.04")
PROBE_TIMEOUT = int(os.getenv("HW_PROBE_TIMEOUT", "120"))  # incl. first-time image pull

# Config-apply (Phase F). When the setup wizard writes new tier/GPU values
# to .env, the dashboard pubs a list of services that need recreating. We
# handle that by `compose up -d --force-recreate <svcs>`.
CONFIG_APPLY_CHANNEL = "config:apply"
# Only services in this allowlist may be force-recreated by the wizard.
# Anything outside is silently ignored to bound blast radius.
CONFIG_APPLY_ALLOWED_SERVICES = {
    "pose-detector", "vehicle-detector", "face-recognizer",
    "camera-ingester", "ollama", "dashboard",
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("orchestrator")


# ---------------------------------------------------------------------------
# Compose CLI helpers
# ---------------------------------------------------------------------------
def _compose_base_cmd() -> list:
    """Compose CLI args common to every invocation."""
    return [
        "docker", "compose",
        "-f", CONTAINER_COMPOSE_FILE,
        "--project-directory", HOST_PROJECT_DIR,
        "-p", COMPOSE_PROJECT_NAME,
    ]


def _run_compose(extra_args: list, timeout: int) -> tuple[bool, str]:
    """Run a `docker compose …` invocation. Returns (success, stderr_tail).

    stderr_tail is the last line of stderr trimmed to a sensible length;
    handy for showing in the dashboard audit feed without flooding the UI.
    """
    cmd = _compose_base_cmd() + extra_args
    logger.info(f"$ {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode == 0:
            return True, ""
        err_lines = (result.stderr or "").strip().splitlines()
        return False, (err_lines[-1] if err_lines else "non-zero exit")[:300]
    except subprocess.TimeoutExpired:
        return False, f"timeout after {timeout}s"
    except FileNotFoundError:
        return False, "docker CLI not found in container"
    except Exception as e:
        return False, str(e)[:300]


def compose_up_profile(r: redis.Redis, profile: str) -> None:
    """Bring a slot up by targeting ONLY its services.

    `docker compose --profile X up -d` (with no service list) brings up the
    profile's services AND every service without a profile — which means
    it tries to recreate the dashboard, the orchestrator itself, and so on.
    That breaks things. Targeting explicit service names keeps the up
    surgical: only the slot's services start, nothing else is touched.
    """
    if profile not in ALLOWED_PROFILES:
        logger.warning(f"Refused to up unknown profile: {profile!r}")
        _audit(r, "up", profile, False, "profile not in allowlist")
        return

    services = _services_for_profile(profile)
    if not services:
        logger.warning(f"Profile {profile} has no services to start (config lookup empty)")
        _audit(r, "up", profile, False, "no services found for profile")
        return

    logger.info(f"Bringing up profile {profile}: services={services}")
    ok, err = _run_compose(
        ["--profile", profile, "up", "-d", "--no-recreate"] + services,
        timeout=180,
    )
    _audit(r, "up", profile, ok, err)


def _services_for_profile(profile: str) -> list:
    """Return the docker-compose service names that belong to this profile.

    We can't rely on `docker compose --profile X down` because compose's
    `down` doesn't actually scope to the profile flag the way `up` does —
    and adding `--remove-orphans` makes it WORSE (treats every non-profile
    service in the project as an orphan and removes them). So we enumerate
    the slot's services by name (every block has a `-{profile}` suffix in
    its key) and stop/remove them individually.
    """
    cmd = _compose_base_cmd() + ["--profile", profile, "config", "--services"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except Exception as e:
        logger.warning(f"config --services for {profile} failed: {e}")
        return []
    if result.returncode != 0:
        logger.warning(f"config --services non-zero: {(result.stderr or '').strip()[:200]}")
        return []
    suffix = f"-{profile}"
    return [s.strip() for s in (result.stdout or "").splitlines()
            if s.strip().endswith(suffix)]


def compose_down_profile(r: redis.Redis, profile: str) -> None:
    """Tear a slot down by stopping + removing only its services.

    DO NOT use `docker compose --profile X down` here — it tears down the
    entire project, not just the profile's services. DO NOT pass
    `--remove-orphans` either — it removes anything outside the current
    profile scope, which means every other service in the project.
    """
    if profile not in ALLOWED_PROFILES:
        logger.warning(f"Refused to down unknown profile: {profile!r}")
        _audit(r, "down", profile, False, "profile not in allowlist")
        return

    services = _services_for_profile(profile)
    if not services:
        logger.info(f"Profile {profile} has no services to stop")
        _audit(r, "down", profile, True, "no services to stop")
        return

    logger.info(f"Tearing down profile {profile}: services={services}")

    # Stop first (gives ffmpeg etc. a chance to flush) then rm.
    ok, err = _run_compose(["stop"] + services, timeout=90)
    if not ok:
        _audit(r, "down", profile, False, f"stop failed: {err}")
        return
    ok, err = _run_compose(["rm", "-f", "-s"] + services, timeout=30)
    _audit(r, "down", profile, ok, err)


# ---------------------------------------------------------------------------
# State queries
# ---------------------------------------------------------------------------
def desired_profiles(r: redis.Redis) -> set:
    """Return the set of profile names that SHOULD be running based on the
    registry — that is, the camera ids that are both enabled AND in
    ALLOWED_PROFILES. All 5 slots (cam1-cam5) are profile-gated; each
    runs only when the registry has its entry."""
    try:
        raw = r.hgetall(REGISTRY_KEY)
    except redis.RedisError as e:
        logger.warning(f"Registry read failed: {e}")
        return set()
    out = set()
    for cid, val in raw.items():
        try:
            entry = json.loads(val)
        except (ValueError, json.JSONDecodeError):
            continue
        if not entry.get("enabled", True):
            continue
        eid = entry.get("id", cid)
        if eid in ALLOWED_PROFILES:
            out.add(eid)
    return out


def running_profiles() -> set:
    """Return the set of allowed-profile names whose services are currently
    running, inferred by listing services and matching their -<slot> suffix."""
    cmd = _compose_base_cmd() + ["ps", "--services", "--filter", "status=running"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except Exception as e:
        logger.warning(f"docker ps failed: {e}")
        return set()
    if result.returncode != 0:
        logger.warning(f"docker ps non-zero: {(result.stderr or '').strip()[:200]}")
        return set()
    out = set()
    for svc in (result.stdout or "").strip().splitlines():
        svc = svc.strip()
        for slot in ALLOWED_PROFILES:
            if svc.endswith(f"-{slot}"):
                out.add(slot)
                break
    return out


# ---------------------------------------------------------------------------
# Audit + reconcile
# ---------------------------------------------------------------------------
def _audit(r: redis.Redis, action: str, profile: str, success: bool, detail: str = "") -> None:
    """Append an entry to the audit stream so the dashboard can show status."""
    try:
        r.xadd(AUDIT_STREAM, {
            "action": action,
            "profile": profile,
            "success": "1" if success else "0",
            "detail": detail,
            "timestamp": str(time.time()),
        }, maxlen=AUDIT_MAXLEN)
    except redis.RedisError as e:
        logger.warning(f"Audit write failed: {e}")


def reconcile(r: redis.Redis) -> None:
    """Compare desired vs running profiles and apply the diff."""
    desired = desired_profiles(r)
    actual = running_profiles()

    to_start = sorted(desired - actual)
    to_stop = sorted(actual - desired)

    if to_start or to_stop:
        logger.info(
            f"Reconcile: desired={sorted(desired)} actual={sorted(actual)} "
            f"start={to_start} stop={to_stop}"
        )

    for profile in to_start:
        compose_up_profile(r, profile)
    for profile in to_stop:
        compose_down_profile(r, profile)


# ---------------------------------------------------------------------------
# Hardware probe — Phase D setup wizard support
# ---------------------------------------------------------------------------
def _run_hardware_probe() -> dict:
    """Spawn a one-shot nvidia/cuda container and parse nvidia-smi output.

    Returns a dict shaped like:
        {"gpus": [{"index": 0, "name": "RTX 3060", "vram_mb": 12288}, ...]}
    or
        {"gpus": [], "error": "<message>"}

    We use `docker run --rm --gpus all` so this works regardless of which
    GPUs the orchestrator itself has access to. The probe image is small
    (~200 MB) and pulled lazily on first wizard run.
    """
    cmd = [
        "docker", "run", "--rm", "--gpus", "all",
        PROBE_IMAGE,
        "nvidia-smi",
        "--query-gpu=index,name,memory.total",
        "--format=csv,noheader,nounits",
    ]
    logger.info(f"Hardware probe: $ {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"gpus": [], "error": f"probe timed out after {PROBE_TIMEOUT}s"}
    except FileNotFoundError:
        return {"gpus": [], "error": "docker CLI not found in orchestrator container"}
    except Exception as e:
        return {"gpus": [], "error": f"probe spawn failed: {e}"}

    if result.returncode != 0:
        err = (result.stderr or "").strip().splitlines()
        tail = err[-1] if err else f"non-zero exit ({result.returncode})"
        return {"gpus": [], "error": tail[:300]}

    gpus = []
    for line in (result.stdout or "").strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            gpus.append({
                "index": int(parts[0]),
                "name": parts[1],
                "vram_mb": int(parts[2]),
            })
        except (ValueError, IndexError):
            continue

    if not gpus:
        return {"gpus": [], "error": "nvidia-smi returned no GPUs"}
    return {"gpus": gpus}


def probe_listen_loop(r: redis.Redis) -> None:
    """Subscribe to setup:probe-request and run nvidia-smi on each message.

    Result lands on the setup:probe-result stream so the dashboard's
    waiting /api/setup/detect-hardware call can pick it up.
    """
    while True:
        try:
            pubsub = r.pubsub(ignore_subscribe_messages=True)
            pubsub.subscribe(PROBE_REQUEST_CHANNEL)
            logger.info(f"Subscribed to {PROBE_REQUEST_CHANNEL}")
            for msg in pubsub.listen():
                if msg.get("type") != "message":
                    continue
                try:
                    req = json.loads(msg.get("data") or "{}")
                except (ValueError, json.JSONDecodeError):
                    continue
                request_id = req.get("request_id", "")
                logger.info(f"Hardware probe request received (id={request_id})")
                payload = _run_hardware_probe()
                try:
                    r.xadd(
                        PROBE_RESULT_STREAM,
                        {"request_id": request_id, "payload": json.dumps(payload)},
                        maxlen=PROBE_RESULT_MAXLEN,
                    )
                    logger.info(f"Probe result published (id={request_id}, gpus={len(payload.get('gpus', []))})")
                    _audit(r, "probe", "host", "error" not in payload,
                           f"{len(payload.get('gpus', []))} GPU(s)" if "error" not in payload else payload["error"])
                except redis.RedisError as e:
                    logger.warning(f"Couldn't publish probe result: {e}")
        except redis.RedisError as e:
            logger.warning(f"Probe pubsub disconnected ({e}); reconnecting in 5s")
            time.sleep(5)


# ---------------------------------------------------------------------------
# Config-apply listener — recreate services on .env changes
# ---------------------------------------------------------------------------
def apply_config(r: redis.Redis, services: list, request_id: str) -> None:
    """Force-recreate the specified services so they pick up new env values.

    Filters against CONFIG_APPLY_ALLOWED_SERVICES so a malformed/malicious
    message can't try to recreate arbitrary services.
    """
    valid = [s for s in services if s in CONFIG_APPLY_ALLOWED_SERVICES]
    rejected = [s for s in services if s not in CONFIG_APPLY_ALLOWED_SERVICES]
    if rejected:
        logger.warning(f"config:apply ignoring {rejected} (not in allowlist)")

    if not valid:
        logger.info(f"config:apply {request_id}: no valid services to restart")
        _audit(r, "apply", "config", True, "no services")
        return

    logger.info(f"config:apply {request_id}: recreating {valid}")
    # `up -d --force-recreate <services>` recreates the named services
    # with their current env vars from compose+.env. Other services stay put.
    ok, err = _run_compose(
        ["up", "-d", "--force-recreate", "--no-deps"] + valid,
        timeout=240,
    )
    _audit(r, "apply", "config", ok, f"{','.join(valid)}" + (f" — {err}" if err else ""))


def config_apply_listen_loop(r: redis.Redis) -> None:
    """Subscribe to config:apply and recreate listed services per message."""
    while True:
        try:
            pubsub = r.pubsub(ignore_subscribe_messages=True)
            pubsub.subscribe(CONFIG_APPLY_CHANNEL)
            logger.info(f"Subscribed to {CONFIG_APPLY_CHANNEL}")
            for msg in pubsub.listen():
                if msg.get("type") != "message":
                    continue
                try:
                    payload = json.loads(msg.get("data") or "{}")
                except (ValueError, json.JSONDecodeError):
                    continue
                services = payload.get("services", []) or []
                request_id = payload.get("request_id", "")
                if not isinstance(services, list):
                    logger.warning(f"config:apply: services field must be list, got {type(services)}")
                    continue
                apply_config(r, services, request_id)
        except redis.RedisError as e:
            logger.warning(f"config:apply pubsub disconnected ({e}); reconnecting in 5s")
            time.sleep(5)


# ---------------------------------------------------------------------------
# Event listener (pub/sub) — kicks reconcile immediately on dashboard CRUD
# ---------------------------------------------------------------------------
def listen_loop(r: redis.Redis) -> None:
    """Subscribe to cameras:events and reconcile on every message.

    The payload format is intentionally simple — we don't trust the
    contents, we always reconcile against the registry. The message
    just signals 'something changed, look now'.
    """
    while True:
        try:
            pubsub = r.pubsub(ignore_subscribe_messages=True)
            pubsub.subscribe(EVENTS_CHANNEL)
            logger.info(f"Subscribed to {EVENTS_CHANNEL}")
            for msg in pubsub.listen():
                if msg.get("type") != "message":
                    continue
                logger.info(f"Event nudge: {msg.get('data')!r}")
                reconcile(r)
        except redis.RedisError as e:
            logger.warning(f"Pubsub disconnected ({e}); reconnecting in 5s")
            time.sleep(5)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    logger.info(f"Orchestrator starting. Allowed profiles: {sorted(ALLOWED_PROFILES)}")
    logger.info(f"Host project dir: {HOST_PROJECT_DIR}")
    logger.info(f"Compose project name: {COMPOSE_PROJECT_NAME}")

    if not ALLOWED_PROFILES:
        logger.error("ALLOWED_PROFILES is empty — nothing to orchestrate. Exiting.")
        sys.exit(1)

    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    for attempt in range(30):
        try:
            r.ping()
            break
        except redis.ConnectionError:
            logger.info(f"Waiting for Redis... attempt {attempt + 1}/30")
            time.sleep(2)
    else:
        logger.error("Could not reach Redis after 60s; exiting.")
        sys.exit(1)

    # Pub/sub listener on a daemon thread; uses its own Redis connection
    # so the blocking listen() doesn't tie up the main reconcile loop.
    listener_r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    t = threading.Thread(target=listen_loop, args=(listener_r,), daemon=True)
    t.start()

    # Hardware probe listener — separate thread + connection so the (slow)
    # nvidia-smi spawn doesn't block camera-event handling.
    probe_r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    probe_t = threading.Thread(target=probe_listen_loop, args=(probe_r,), daemon=True)
    probe_t.start()

    # Config-apply listener — recreates services when .env changes (Phase F)
    cfg_r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    cfg_t = threading.Thread(target=config_apply_listen_loop, args=(cfg_r,), daemon=True)
    cfg_t.start()

    # Safety-net reconcile loop
    while True:
        try:
            reconcile(r)
        except Exception as e:
            logger.warning(f"Reconcile error: {e}")
        time.sleep(RECONCILE_INTERVAL)


if __name__ == "__main__":
    main()
