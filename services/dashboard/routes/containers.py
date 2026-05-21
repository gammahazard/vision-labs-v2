"""
routes/containers.py — Read-only view of all project containers.

PURPOSE:
    Powers the "Containers" tab on the monitoring page. Returns a
    snapshot of every container in the vision-labs compose project
    so the user can see what's up/down without leaving the dashboard
    or opening Portainer.

RELATIONSHIPS:
    - Reads `orchestrator:containers` Redis key (set by the orchestrator
      every reconcile pass; 60 s TTL).
    - Dashboard does NOT have Docker socket access by design — we get
      the snapshot from the orchestrator instead.

PROXY VS DIRECT:
    The Docker socket lives only on the orchestrator container for
    security. This endpoint deliberately can't run/stop containers —
    it's read-only. To actually manage containers, the user clicks
    through to Portainer (also has the socket, in its own access UI).
"""
import json
import logging
import time

import redis
from fastapi import APIRouter
from fastapi.responses import JSONResponse

import routes as ctx

logger = logging.getLogger("dashboard.containers")

router = APIRouter(prefix="/api", tags=["containers"])


@router.get("/containers")
async def list_containers():
    """Return the orchestrator's container snapshot.

    Shape:
        {
          "ok": true,
          "stale": false,         # True if generated_at > 30s ago
          "age_seconds": float,
          "project": "vision-labs",
          "containers": [
            {"name": "vision-labs-dashboard-1", "service": "dashboard",
             "state": "running", "status": "Up 4 hours",
             "health": "healthy", "image": "...", "exit_code": 0},
            ...
          ]
        }
    """
    try:
        raw = ctx.r.get("orchestrator:containers")
    except redis.RedisError:
        logger.exception("Redis error fetching container state")
        return JSONResponse(
            status_code=503,
            content={"ok": False, "error": "Redis unreachable — see dashboard logs for details"},
        )
    if not raw:
        # TTL expired — orchestrator probably down.
        return {
            "ok": False,
            "stale": True,
            "error": "Orchestrator hasn't published container state recently. "
                     "Check `docker logs vision-labs-orchestrator-1`.",
            "containers": [],
        }
    try:
        payload = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": "Invalid container snapshot"},
        )
    age = time.time() - payload.get("generated_at", 0)
    return {
        "ok": True,
        "stale": age > 30,
        "age_seconds": round(age, 1),
        "project": payload.get("project", ""),
        "containers": payload.get("containers", []),
    }
