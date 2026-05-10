"""
routes/telegram_access.py — Telegram Access Manager REST endpoints.

PURPOSE:
    Manage authorized Telegram bot users and view the access log.
    Users are stored in Redis (TELEGRAM_USERS_KEY hash) and the access log
    is a Redis stream (TELEGRAM_ACCESS_LOG).

ENDPOINTS:
    GET    /api/telegram/users         — list all approved users
    POST   /api/telegram/users         — approve a user
    DELETE /api/telegram/users/{uid}   — revoke a user
    GET    /api/telegram/access-log    — recent access attempts
    DELETE /api/telegram/access-log    — clear the log
"""

import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter
from fastapi.responses import JSONResponse

import routes as ctx

router = APIRouter(prefix="/api/telegram", tags=["telegram-access"])


@router.get("/users")
async def list_users():
    """List all approved Telegram bot users."""
    raw = ctx.r.hgetall(ctx.TELEGRAM_USERS_KEY)
    users = {}
    for uid_bytes, meta_bytes in raw.items():
        uid = uid_bytes if isinstance(uid_bytes, str) else uid_bytes.decode()
        meta = meta_bytes if isinstance(meta_bytes, str) else meta_bytes.decode()
        try:
            users[uid] = json.loads(meta)
        except (json.JSONDecodeError, TypeError):
            users[uid] = {"name": meta, "chat_id": "", "username": ""}
    return {"users": users}


@router.post("/users")
async def approve_user(user_id: str, chat_id: str = "",
                       name: str = "", username: str = "",
                       role: str = "user"):
    """Approve a Telegram user for bot access."""
    if not user_id.strip().isdigit():
        return JSONResponse(status_code=400,
                            content={"error": "user_id must be numeric"})

    # Validate role
    if role not in ("admin", "user"):
        role = "user"

    meta = json.dumps({
        "chat_id": chat_id,
        "name": name,
        "username": username,
        "role": role,
        "approved_at": datetime.now(
            ZoneInfo(os.getenv("LOCATION_TIMEZONE", "America/Toronto"))
        ).strftime("%Y-%m-%d %H:%M"),
    })
    ctx.r.hset(ctx.TELEGRAM_USERS_KEY, user_id.strip(), meta)
    ctx.logger.info(f"Telegram user approved: {user_id} ({name}) role={role}")
    return {"status": "approved", "user_id": user_id}


@router.delete("/users/{user_id}")
async def revoke_user(user_id: str):
    """Revoke a Telegram user's bot access."""
    removed = ctx.r.hdel(ctx.TELEGRAM_USERS_KEY, user_id)
    if removed:
        ctx.logger.info(f"Telegram user revoked: {user_id}")
        return {"status": "revoked", "user_id": user_id}
    return JSONResponse(status_code=404,
                        content={"error": f"User {user_id} not found"})


@router.get("/access-log")
async def get_access_log(count: int = 50):
    """Get recent Telegram access attempts, newest first."""
    count = min(count, 200)
    entries = ctx.r.xrevrange(ctx.TELEGRAM_ACCESS_LOG, count=count)
    log = []
    for msg_id, data in entries:
        entry = {}
        for k, v in data.items():
            key = k if isinstance(k, str) else k.decode()
            val = v if isinstance(v, str) else v.decode()
            entry[key] = val
        entry["_id"] = msg_id if isinstance(msg_id, str) else msg_id.decode()
        log.append(entry)
    return {"log": log}


@router.delete("/access-log")
async def clear_access_log():
    """Clear the Telegram access log."""
    ctx.r.delete(ctx.TELEGRAM_ACCESS_LOG)
    ctx.logger.info("Telegram access log cleared")
    return {"status": "cleared"}
