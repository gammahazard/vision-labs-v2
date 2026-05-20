"""
routes/notifications/_shared.py — config, helpers, and constants shared across the package.

Owns:
  - Telegram + Redis + Ollama env vars
  - HTML-escape (_esc) + token redaction (_redact_token)
  - Per-camera cooldown helpers (Redis-backed)
  - User authorization gate (_is_authorized) and chat-ID broadcast list

These are the module-level constants the legacy monolith had at the top;
every other file in the package imports from here.
"""

import os
import html
import json
import logging
from datetime import datetime

import redis
from contracts.redis_client import make_redis_client
from contracts.tz import TZ_LOCAL  # validated single source of truth
from constants import OLLAMA_HOST, VISION_MODEL, OLLAMA_KEEP_ALIVE

import routes as ctx

logger = logging.getLogger("dashboard.notifications")

# Telegram config — read from environment
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# Security — seed users from env var (migrated to Redis at startup)
TELEGRAM_ALLOWED_USERS: set[int] = {
    int(uid.strip())
    for uid in os.getenv("TELEGRAM_ALLOWED_USERS", "").split(",")
    if uid.strip().isdigit()
}

# Redis config — for binary frame reads
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

# Rate-limit timestamps live in a Redis hash so a dashboard restart can't
# bypass an active cooldown. Keyed by event-type:camera_id.
_COOLDOWN_KEY = "notify:last"

def _esc(value) -> str:
    """HTML-escape any user-controlled string before it lands in a Telegram
    caption sent with parse_mode=HTML.

    Captions used to interpolate `identity_name`, `zone`, `action`,
    `vehicle_class`, and AI descriptions raw. A face enrolled with a
    name like `<unclosed` was enough to make Telegram return 400 — and
    `send_photo` would silently drop the whole notification. This helper
    makes that an impossible failure mode and incidentally blocks any
    HTML injection from a malicious face name.
    """
    if value is None:
        return ""
    return html.escape(str(value), quote=False)


def _redact_token(text: str) -> str:
    """Strip the bot token from any string before logging it.

    Telegram API error responses occasionally echo the token back in URLs
    or in the request-line of error messages. Logging resp.text raw would
    leak the token into stdout, container logs, and any log aggregator
    downstream. This helper redacts:
      - The exact configured token (if non-empty)
      - Any generic bot-token-shaped string (`\\d+:[A-Za-z0-9_-]{30,}`)
    """
    import re as _re
    if not text:
        return text
    out = text
    if TELEGRAM_BOT_TOKEN:
        out = out.replace(TELEGRAM_BOT_TOKEN, "[bot_token_redacted]")
    # Generic shape — catches a different token if env was rotated mid-run
    out = _re.sub(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b", "[bot_token_redacted]", out)
    return out


def _get_cooldown(key: str, default: int) -> int:
    """Read a cooldown value from Redis config, falling back to default."""
    try:
        val = ctx.r.hget(ctx.CONFIG_KEY, key)
        if val:
            return max(10, int(float(val)))  # Floor at 10s to prevent spam
    except Exception:
        pass
    return default


def _cooldown_field(event_type: str, camera_id: str = "") -> str:
    """Per-camera cooldown field. Returns `event_type:camera_id` so each
    camera tracks its own cooldown — the global `event_type` key meant
    a person event on cam1 would suppress notifications from cam2-5 for
    the next 60s. Falls back to the bare event_type when no camera_id
    is provided (legacy callers / global gates)."""
    cid = (camera_id or "").strip()
    return f"{event_type}:{cid}" if cid else event_type


def _get_last_notification(event_type: str, camera_id: str = "") -> float:
    """Return the last-broadcast timestamp for the given event type + camera, 0 if never."""
    try:
        val = ctx.r.hget(_COOLDOWN_KEY, _cooldown_field(event_type, camera_id))
        return float(val) if val else 0.0
    except (redis.RedisError, ValueError, TypeError):
        return 0.0


def _set_last_notification(event_type: str, ts: float, camera_id: str = "") -> None:
    """Record the last-broadcast timestamp for `event_type` on `camera_id`.

    Best-effort — failure to write just falls back to the previous behavior
    (in-process counter), so we swallow Redis errors rather than crashing
    the notification path.
    """
    try:
        ctx.r.hset(_COOLDOWN_KEY, _cooldown_field(event_type, camera_id), str(ts))
    except redis.RedisError as e:
        logger.warning(f"Failed to persist cooldown timestamp: {e}")


def _now_str() -> str:
    """Get the current time formatted in local timezone."""
    return datetime.now(TZ_LOCAL).strftime("%I:%M:%S %p")


def is_configured() -> bool:
    """Check if Telegram bot token and chat ID are both set."""
    return bool(TELEGRAM_BOT_TOKEN) and bool(TELEGRAM_CHAT_ID)


def _is_authorized(user_id: int | None, chat_id: int | None) -> bool:
    """
    Security gate — checks if user is approved in Redis.

    Falls back to TELEGRAM_ALLOWED_USERS env var + TELEGRAM_CHAT_ID
    if Redis hash is not yet populated (bootstrap compatibility).
    """
    if not user_id or not chat_id:
        return False

    uid_str = str(user_id)

    # Primary: check Redis hash
    if ctx.TELEGRAM_USERS_KEY and ctx.r:
        if ctx.r.hexists(ctx.TELEGRAM_USERS_KEY, uid_str):
            return True

    # Fallback: env var whitelist + chat ID check (pre-migration)
    if TELEGRAM_ALLOWED_USERS and user_id in TELEGRAM_ALLOWED_USERS:
        if str(chat_id) == TELEGRAM_CHAT_ID:
            return True

    return False


def _get_all_chat_ids() -> list[str]:
    """
    Get chat IDs for ALL approved Telegram users.
    Used for broadcasting system alerts (person detected, vehicle idle, etc.).
    Falls back to TELEGRAM_CHAT_ID if no users in Redis.
    """
    chat_ids = []
    if ctx.TELEGRAM_USERS_KEY and ctx.r:
        raw = ctx.r.hgetall(ctx.TELEGRAM_USERS_KEY)
        for uid_bytes, meta_bytes in raw.items():
            meta = meta_bytes if isinstance(meta_bytes, str) else meta_bytes.decode()
            try:
                data = json.loads(meta)
                cid = data.get("chat_id", "")
                if cid:
                    chat_ids.append(str(cid))
            except (json.JSONDecodeError, TypeError):
                pass
    # Fallback: primary admin chat
    if not chat_ids and TELEGRAM_CHAT_ID:
        chat_ids.append(TELEGRAM_CHAT_ID)
    return chat_ids
