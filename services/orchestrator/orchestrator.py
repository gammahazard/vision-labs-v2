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
import re
import signal
import subprocess
import sys
import time
import threading

# contracts/ isn't COPY'd into this image (Dockerfile keeps it small —
# just docker:24-cli + python3). Pick it up from the project mount at
# /workspace, which docker-compose.yml already bind-mounts read-only.
sys.path.insert(0, "/workspace")

import redis
from contracts.redis_client import make_redis_client


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
# Raised from 500 → 2000: with 5 cameras + periodic reconciles + apply/probe
# rows, a busy day burned through 500 in under an hour. The dashboard's
# "live status" timeline reads this stream, so a wider window is useful.
AUDIT_MAXLEN = 2000

# Setup-wizard hardware probe (Phase D). Dashboard publishes a request,
# we spawn a one-shot nvidia-smi container and stream the result back.
PROBE_REQUEST_CHANNEL = "setup:probe-request"
PROBE_RESULT_STREAM = "setup:probe-result"
PROBE_RESULT_MAXLEN = 50  # tiny — probe payloads are small + only consumed once
# CUDA 12.8 is required for the Blackwell architecture (5070 Ti). The
# previous default of 12.4.0 would silently fail to detect newer cards;
# the probe would report "no GPUs" precisely when we most need to see them.
PROBE_IMAGE = os.getenv("HW_PROBE_IMAGE", "nvidia/cuda:12.8.0-base-ubuntu22.04")
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
    # recorder needs restart for RETENTION_DAYS changes; snapshot + clip
    # retention is handled by dashboard's hot-reload poller so no restart
    # needed for those two.
    "recorder",
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
# Concurrency + shutdown state
# ---------------------------------------------------------------------------
# Mutex around reconcile so the periodic loop and the cameras:events
# listener can't run two reconciles at once. Without this we observed
# audit log noise where two reconciles racing on the same `down` produce
# `success=0, detail="removal of container ... is already in progress"`.
_RECONCILE_LOCK = threading.Lock()

# Flipped by the SIGTERM handler so the reconcile loop exits cleanly.
_SHUTDOWN = threading.Event()


# Credential-scrubbing regex for audit details. compose/build stderr can
# echo RTSP URLs with user:pass embedded; the audit stream is consumed
# by the dashboard's status panel and would otherwise leak creds to any
# logged-in user.
_RTSP_CRED_RE = re.compile(r"(rtsp[s]?://)[^@\s/]+@", re.IGNORECASE)


def _scrub_creds(text: str) -> str:
    """Replace `user:pass@` in any RTSP URL inside `text` with `***@`."""
    if not text:
        return text
    return _RTSP_CRED_RE.sub(r"\1***@", text)


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
def desired_profiles(r: redis.Redis) -> set | None:
    """Return the set of profile names that SHOULD be running based on the
    registry — that is, the camera ids that are both enabled AND in
    ALLOWED_PROFILES. All 5 slots (cam1-cam5) are profile-gated; each
    runs only when the registry has its entry.

    Returns None on Redis error (sentinel for "I don't know; skip this
    reconcile"). Previously this returned an empty set on error, which
    `reconcile` then interpreted as "stop everything" — a transient
    Redis hiccup would tear down every camera.
    """
    try:
        raw = r.hgetall(REGISTRY_KEY)
    except redis.RedisError as e:
        logger.warning(f"Registry read failed: {e}")
        return None
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
def _audit(r: redis.Redis, action: str, profile: str, success: bool,
           detail: str = "", request_id: str = "") -> None:
    """Append an entry to the audit stream so the dashboard can show status.

    `detail` is credential-scrubbed (any `rtsp://user:pass@` is masked)
    so a build error or compose stderr can't leak camera credentials
    through the dashboard status feed.

    `request_id` is optional and echoed verbatim so the dashboard can
    correlate audit rows with the apply/probe call that triggered them.
    """
    fields = {
        "action": action,
        "profile": profile,
        "success": "1" if success else "0",
        "detail": _scrub_creds(detail),
        "timestamp": str(time.time()),
    }
    if request_id:
        fields["request_id"] = request_id
    try:
        r.xadd(AUDIT_STREAM, fields, maxlen=AUDIT_MAXLEN)
    except redis.RedisError as e:
        logger.warning(f"Audit write failed: {e}")


def reconcile(r: redis.Redis) -> None:
    """Compare desired vs running profiles and apply the diff.

    Serialized on `_RECONCILE_LOCK` so the periodic safety-net pass and
    the cameras:events listener can't run two reconciles at once. We saw
    this fire in production — two near-simultaneous `down` invocations
    on the same profile produced spurious audit entries like
    "removal of container ... is already in progress".
    """
    with _RECONCILE_LOCK:
        desired = desired_profiles(r)
        if desired is None:
            # Sentinel: registry unreadable. Skip the pass rather than
            # treat an empty desired-set as "stop everything." Next tick
            # (or pubsub nudge) will retry.
            return
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

        # Publish a snapshot of every project container's state so the
        # dashboard's Containers tab can render without needing its own
        # Docker socket. Best-effort: a failure here doesn't break the
        # reconcile pass that's the actual job of this function.
        try:
            _publish_container_state(r)
        except Exception as e:
            logger.debug(f"Container state publish failed: {e}")


def _publish_container_state(r: redis.Redis) -> None:
    """Snapshot all project containers via `docker compose ps` and store
    the result as JSON in `orchestrator:containers` (60 s TTL).

    The dashboard's `/api/containers` reads this. We use a TTL so a
    dead orchestrator can't keep a stale list around forever; the
    Containers tab will show "orchestrator offline" if the key is gone.
    """
    cmd = _compose_base_cmd() + ["ps", "-a", "--format", "json"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except Exception as e:
        logger.debug(f"docker compose ps failed: {e}")
        return
    if result.returncode != 0:
        return
    containers = []
    # Compose v2 emits one JSON object per line
    for line in (result.stdout or "").strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, json.JSONDecodeError):
            continue
        # Keep just the fields the UI needs — keeps the payload tiny
        # and avoids leaking command-line args or env into the response.
        containers.append({
            "name": obj.get("Name", ""),
            "service": obj.get("Service", ""),
            "state": obj.get("State", ""),
            "status": obj.get("Status", ""),
            "health": obj.get("Health", ""),
            "image": obj.get("Image", ""),
            "exit_code": obj.get("ExitCode", 0),
        })
    containers.sort(key=lambda c: c["name"])
    payload = json.dumps({
        "containers": containers,
        "generated_at": time.time(),
        "project": COMPOSE_PROJECT_NAME,
    })
    try:
        r.setex("orchestrator:containers", 60, payload)
    except redis.RedisError as e:
        logger.debug(f"setex orchestrator:containers failed: {e}")


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
                    _audit(
                        r, "probe", "host",
                        "error" not in payload,
                        f"{len(payload.get('gpus', []))} GPU(s)" if "error" not in payload else payload["error"],
                        request_id=request_id,
                    )
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
        _audit(r, "apply", "config", True, "no services", request_id=request_id)
        return

    logger.info(f"config:apply {request_id}: recreating {valid}")
    # `up -d --force-recreate <services>` recreates the named services
    # with their current env vars from compose+.env. Other services stay put.
    ok, err = _run_compose(
        ["up", "-d", "--force-recreate", "--no-deps"] + valid,
        timeout=240,
    )
    _audit(
        r, "apply", "config", ok,
        f"{','.join(valid)}" + (f" — {err}" if err else ""),
        request_id=request_id,
    )


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
def _install_shutdown_handlers() -> None:
    """Trap SIGTERM/SIGINT and flip _SHUTDOWN.

    The main reconcile loop checks _SHUTDOWN on every iteration so it
    can exit cleanly. In-flight `docker compose` subprocesses are NOT
    interrupted — they're managed by the daemon and will finish on their
    own; we just stop initiating new ones. Daemon listener threads die
    when main returns.
    """
    def _handler(signum, _frame):
        logger.info(f"Received signal {signum} — draining and exiting")
        _SHUTDOWN.set()
    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def main() -> None:
    logger.info(f"Orchestrator starting. Allowed profiles: {sorted(ALLOWED_PROFILES)}")
    logger.info(f"Host project dir: {HOST_PROJECT_DIR}")
    logger.info(f"Compose project name: {COMPOSE_PROJECT_NAME}")

    if not ALLOWED_PROFILES:
        logger.error("ALLOWED_PROFILES is empty — nothing to orchestrate. Exiting.")
        sys.exit(1)

    _install_shutdown_handlers()

    r = make_redis_client(decode_responses=True, host=REDIS_HOST, port=REDIS_PORT)
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
    listener_r = make_redis_client(decode_responses=True, host=REDIS_HOST, port=REDIS_PORT)
    t = threading.Thread(target=listen_loop, args=(listener_r,), daemon=True)
    t.start()

    # Hardware probe listener — separate thread + connection so the (slow)
    # nvidia-smi spawn doesn't block camera-event handling.
    probe_r = make_redis_client(decode_responses=True, host=REDIS_HOST, port=REDIS_PORT)
    probe_t = threading.Thread(target=probe_listen_loop, args=(probe_r,), daemon=True)
    probe_t.start()

    # Config-apply listener — recreates services when .env changes (Phase F)
    cfg_r = make_redis_client(decode_responses=True, host=REDIS_HOST, port=REDIS_PORT)
    cfg_t = threading.Thread(target=config_apply_listen_loop, args=(cfg_r,), daemon=True)
    cfg_t.start()

    # Safety-net reconcile loop. `_SHUTDOWN.wait(timeout=N)` is a
    # responsive sleep — SIGTERM unblocks immediately instead of
    # waiting up to RECONCILE_INTERVAL seconds.
    while not _SHUTDOWN.is_set():
        try:
            reconcile(r)
        except Exception as e:
            logger.warning(f"Reconcile error: {e}")
        if _SHUTDOWN.wait(timeout=RECONCILE_INTERVAL):
            break
    logger.info("Orchestrator main loop exited.")


if __name__ == "__main__":
    main()
