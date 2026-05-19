"""
contracts/redis_client.py — Single-source-of-truth helper for building
authenticated Redis clients.

PURPOSE:
    Centralize the password + host + port resolution so every service builds
    Redis clients the same way. Avoids the 23 sites that previously each
    constructed `redis.Redis(host=..., port=..., decode_responses=...)` and
    would each need to be touched separately if config changed.

USAGE:
    from contracts.redis_client import make_redis_client
    r = make_redis_client(decode_responses=True)
    r_bin = make_redis_client(decode_responses=False)

AUTH BEHAVIOR:
    - If REDIS_PASSWORD is set (non-empty), the client authenticates.
    - If REDIS_PASSWORD is empty/unset, the client connects without auth
      (current default, preserves backward compatibility).
    - When enabling Redis AUTH for the first time, set REDIS_PASSWORD in .env
      AND in docker-compose.yml the Redis service must be started with
      `--requirepass $${REDIS_PASSWORD}` so the server demands it.

WHY OPT-IN:
    Forced password rotation breaks every running deployment until every
    service has the password. Empty-default lets the refactor land
    independently of the actual auth flip — operator decides when to flip.
"""

import os

import redis as _redis


def make_redis_client(
    decode_responses: bool = True,
    host: str | None = None,
    port: int | None = None,
    db: int = 0,
) -> "_redis.Redis":
    """Build a Redis client with consistent host/port/password resolution.

    Args:
        decode_responses: True for text data, False for binary (JPEG frames).
        host: Override REDIS_HOST env (default 'redis').
        port: Override REDIS_PORT env (default 6379).
        db: Redis DB number (default 0).

    Returns:
        A redis.Redis instance, authenticated if REDIS_PASSWORD is set.
    """
    resolved_host = host or os.getenv("REDIS_HOST", "redis")
    resolved_port = port if port is not None else int(os.getenv("REDIS_PORT", "6379"))
    password = os.getenv("REDIS_PASSWORD", "") or None
    return _redis.Redis(
        host=resolved_host,
        port=resolved_port,
        db=db,
        password=password,
        decode_responses=decode_responses,
    )
