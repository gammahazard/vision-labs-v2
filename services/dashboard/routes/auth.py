"""
routes/auth.py — Authentication routes for the Vision Labs dashboard.

PURPOSE:
    Provides login, logout, and password management endpoints.
    Credentials are stored in a SQLite database with salted SHA-256 hashes.
    Sessions use signed cookies — no external dependencies needed.

RELATIONSHIPS:
    - Used by: server.py (middleware checks session cookie)
    - DB: /data/auth.db (Docker volume, persists across restarts)
    - Default: admin/admin (created on first boot if DB is empty)

ENDPOINTS:
    POST /api/auth/login          — validate credentials, set session cookie
    POST /api/auth/logout         — clear session cookie
    POST /api/auth/change-password — update credentials (requires current password)
    GET  /api/auth/status         — check if current session is valid
"""

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api/auth", tags=["auth"])

# secure=True on the session cookie causes browsers to refuse it over plain
# HTTP, which would lock out the typical LAN-only setup (e.g. accessing the
# dashboard at http://192.168.x.y:8080 from a phone). Only set secure=True
# when we know there's a TLS terminator in front of us — operator opts in
# via DASHBOARD_BEHIND_TLS=true.
_COOKIE_SECURE = os.getenv("DASHBOARD_BEHIND_TLS", "false").lower() in ("1", "true", "yes")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Secret key for signing session cookies. Auto-generated if not set.
# Persisted to DB so it survives container restarts.
_SECRET_KEY: str = None
_DB_PATH: str = None

# Minimum password length. 8 is below NIST 2024 guidance (which leans on
# length + denied-list) but high enough to block trivially weak passwords
# without being annoying on a personal LAN install. Bump to 12 if you want
# more friction.
MIN_PASSWORD_LENGTH = 8

# Usernames go into the signed session token (format username:flag:ts:sig) and
# thus into the Set-Cookie value. Constrain to a safe charset so a username
# can't contain ':' (corrupts the token format), CRLF/';' (cookie/header
# injection), or other control chars. Validated everywhere a username flows
# into a token (login + change-password rename).
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

# Brute-force gate state — in-memory dict keyed by client IP.
# {ip: {"fails": int, "window_start": ts, "locked_until": ts}}
# Cleared on container restart, which is acceptable for a single-host
# LAN install; an attacker would need to keep the host up between attempts
# anyway. For HA / multi-process you'd back this with Redis.
_LOGIN_FAILURE_THRESHOLD = 5    # fails within window → lock out
_LOGIN_FAILURE_WINDOW = 300     # 5 minutes
_LOGIN_LOCKOUT_DURATION = 900   # 15 minutes
_login_failures: dict = {}


def _login_rate_check(ip: str) -> tuple[bool, int]:
    """Return (allowed, retry_after_seconds). If allowed is False the
    login route should return 429 immediately, before any DB lookup."""
    now = time.time()
    state = _login_failures.get(ip)
    if state and state.get("locked_until", 0) > now:
        return False, int(state["locked_until"] - now)
    return True, 0


def _record_login_failure(ip: str) -> None:
    """Increment failure count for `ip`. Triggers lockout at threshold."""
    now = time.time()
    state = _login_failures.get(
        ip, {"fails": 0, "window_start": now, "locked_until": 0}
    )
    # Roll the window forward if the previous one expired.
    if now - state["window_start"] > _LOGIN_FAILURE_WINDOW:
        state = {"fails": 0, "window_start": now, "locked_until": 0}
    state["fails"] += 1
    if state["fails"] >= _LOGIN_FAILURE_THRESHOLD:
        state["locked_until"] = now + _LOGIN_LOCKOUT_DURATION
    _login_failures[ip] = state
    # Sweep stale entries opportunistically so the dict can't grow forever.
    if len(_login_failures) > 256:
        cutoff = now - (_LOGIN_FAILURE_WINDOW + _LOGIN_LOCKOUT_DURATION)
        for k in list(_login_failures.keys()):
            v = _login_failures[k]
            if v.get("locked_until", 0) < now and v.get("window_start", 0) < cutoff:
                _login_failures.pop(k, None)


def _reset_login_failures(ip: str) -> None:
    """Clear the failure counter for `ip` after a successful login."""
    _login_failures.pop(ip, None)


def get_db_path() -> str:
    """Get the auth database path from routes context."""
    global _DB_PATH
    if _DB_PATH is None:
        import routes as ctx
        _DB_PATH = getattr(ctx, "AUTH_DB_PATH", "/data/auth.db")
    return _DB_PATH


# ---------------------------------------------------------------------------
# Database Setup
# ---------------------------------------------------------------------------
def _get_db() -> sqlite3.Connection:
    """Get a connection to the auth SQLite database."""
    db = sqlite3.connect(get_db_path())
    db.execute("PRAGMA journal_mode=WAL")
    return db


def init_auth_db():
    """
    Initialize the auth database. Creates tables and default admin user
    if they don't exist. Called once at startup by server.py.
    """
    global _SECRET_KEY

    db = _get_db()
    try:
        # Create tables
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS app_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        db.commit()

        # Load or generate secret key
        row = db.execute(
            "SELECT value FROM app_config WHERE key = 'secret_key'"
        ).fetchone()

        if row:
            _SECRET_KEY = row[0]
        else:
            # Check env first, then generate
            _SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_hex(32))
            db.execute(
                "INSERT INTO app_config (key, value) VALUES ('secret_key', ?)",
                (_SECRET_KEY,),
            )
            db.commit()

        # Create default admin user if no users exist
        count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if count == 0:
            salt = secrets.token_hex(16)
            pw_hash = _hash_password("admin", salt)
            now = time.time()
            db.execute(
                "INSERT INTO users (username, password_hash, salt, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("admin", pw_hash, salt, now, now),
            )
            db.commit()
            import logging
            logging.getLogger("dashboard").info(
                "Created default admin user (admin/admin)"
            )

    finally:
        db.close()


# ---------------------------------------------------------------------------
# Password Hashing
# ---------------------------------------------------------------------------
# bcrypt is the new format ($2b$... prefix). Legacy SHA-256 hashes (64 hex
# chars, separate salt column) are still accepted on login so existing users
# can log in once; the login handler then re-hashes their password as bcrypt
# and overwrites the DB row. Lazy migration — no batch update needed.
import bcrypt as _bcrypt


def _is_bcrypt_hash(stored: str) -> bool:
    return isinstance(stored, str) and stored.startswith(("$2a$", "$2b$", "$2y$"))


def _hash_password_bcrypt(password: str) -> str:
    """Hash a password with bcrypt. Returns the full $2b$... string which
    embeds its own salt + cost factor; the legacy `salt` column becomes
    unused for bcrypt entries (we keep it for SHA-256 compat)."""
    return _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt(rounds=12)).decode("utf-8")


def _hash_password_sha256_legacy(password: str, salt: str) -> str:
    """LEGACY — kept only so _verify_password can compute the legacy hash
    when checking an old SHA-256 entry. New writes go through
    _hash_password_bcrypt."""
    return hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()


def _hash_password(password: str, salt: str = "") -> str:
    """Default to bcrypt for new writes. `salt` is ignored for bcrypt
    (bcrypt manages its own salt). Kept as a kwarg for callsites that
    used to pre-compute a salt — they can keep passing it; we just don't
    use it."""
    return _hash_password_bcrypt(password)


def _verify_password(password: str, salt: str, stored_hash: str) -> bool:
    """Verify a password against the stored hash. Handles both formats:
    bcrypt ($2b$...) and the legacy salted SHA-256. Used by login + change-
    password endpoints. Login also opportunistically upgrades SHA-256 → bcrypt
    on the next successful login (see _maybe_upgrade_to_bcrypt below)."""
    if _is_bcrypt_hash(stored_hash):
        try:
            return _bcrypt.checkpw(password.encode("utf-8"), stored_hash.encode("utf-8"))
        except (ValueError, TypeError):
            return False
    # Legacy path — salted SHA-256
    return hmac.compare_digest(
        _hash_password_sha256_legacy(password, salt), stored_hash
    )


def _maybe_upgrade_to_bcrypt(username: str, password: str, current_hash: str) -> None:
    """Called after a successful legacy-SHA-256 verification. Rewrites the
    DB row with a fresh bcrypt hash so the next login hits the bcrypt fast
    path. Failure is non-fatal — we already let the user in; the next login
    will try again."""
    if _is_bcrypt_hash(current_hash):
        return  # already migrated
    try:
        new_hash = _hash_password_bcrypt(password)
        # Salt column is unused for bcrypt entries; keep the existing value
        # so a downgrade-and-rollback scenario can still parse the row.
        db = _get_db()
        try:
            db.execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE username = ?",
                (new_hash, time.time(), username),
            )
            db.commit()
        finally:
            db.close()
        import logging
        logging.getLogger("dashboard").info(
            f"Migrated password hash for user {username!r} from SHA-256 to bcrypt"
        )
    except Exception as e:
        import logging
        logging.getLogger("dashboard").warning(
            f"bcrypt migration for {username!r} failed (will retry on next login): {e}"
        )


# ---------------------------------------------------------------------------
# Session Tokens
# ---------------------------------------------------------------------------
def _create_session_token(username: str, must_change: bool = False) -> str:
    """
    Create a signed session token: username:flag:timestamp:signature.
    `flag` is "1" when the user is still on default credentials and the
    server should refuse every route except /api/auth/change-password.
    Signature = HMAC-SHA256(secret_key, username:flag:timestamp).
    """
    ts = str(int(time.time()))
    flag = "1" if must_change else "0"
    payload = f"{username}:{flag}:{ts}"
    sig = hmac.new(
        _SECRET_KEY.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    return f"{payload}:{sig}"


def _decode_session(token: str) -> dict | None:
    """Internal: validate signature + expiry and return the full session
    payload as a dict (username, must_change). Used by both the legacy
    `validate_session` wrapper and the new middleware gate.

    Returns None for any failure (bad signature, expired, malformed).
    """
    if not token or not _SECRET_KEY:
        return None

    parts = token.split(":")
    if len(parts) != 4:
        # Reject pre-flag tokens (old 3-part format). Affected users
        # log in again — at most 24 h of stale sessions get invalidated.
        return None

    username, flag_str, ts_str, sig = parts

    payload = f"{username}:{flag_str}:{ts_str}"
    expected_sig = hmac.new(
        _SECRET_KEY.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(sig, expected_sig):
        return None

    try:
        ts = int(ts_str)
        if time.time() - ts > 86400:
            return None
    except ValueError:
        return None

    return {"username": username, "must_change": flag_str == "1"}


def validate_session(token: str) -> str | None:
    """Backward-compat wrapper: return username string for callers that
    don't care about the must_change flag (most of them). New middleware
    that gates routes on must_change should call `_decode_session` directly.
    """
    info = _decode_session(token)
    return info["username"] if info else None


def session_must_change(token: str) -> bool:
    """True iff the current session is flagged "default credentials —
    must change before doing anything". Middleware uses this to gate
    every route except /api/auth/change-password."""
    info = _decode_session(token)
    return bool(info and info["must_change"])


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.post("/login")
async def login(request: Request):
    """Validate credentials and set session cookie.

    Rate limits failed attempts per source IP: 5 fails in 5 min triggers a
    15 min lockout (any password attempt — right or wrong — returns 429 with
    Retry-After during the lockout). Successful login clears the counter."""
    client_ip = request.client.host if request.client else "?"

    # Hard gate BEFORE looking at the DB so a brute-forcer can't even time
    # the bcrypt verify.
    allowed, retry_after = _login_rate_check(client_ip)
    if not allowed:
        return JSONResponse(
            {"error": f"Too many failed attempts. Try again in {retry_after}s."},
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request"}, status_code=400)

    username = body.get("username", "").strip()
    password = body.get("password", "")

    if not username or not password:
        return JSONResponse({"error": "Username and password required"}, status_code=400)

    # A malformed-charset username can't be a real account (change-password
    # enforces the same charset on rename) — reject before it flows into the
    # signed token / Set-Cookie value. Same 401 as bad creds.
    if not _USERNAME_RE.match(username):
        return JSONResponse({"error": "Invalid username or password"}, status_code=401)

    db = _get_db()
    try:
        row = db.execute(
            "SELECT password_hash, salt FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    finally:
        db.close()

    if not row or not _verify_password(password, row[1], row[0]):
        _record_login_failure(client_ip)
        return JSONResponse({"error": "Invalid credentials"}, status_code=401)

    # Correct credentials — clear the per-IP failure counter so future
    # mistyped passwords from this host don't accumulate against earlier
    # attacker traffic that happened to share an IP.
    _reset_login_failures(client_ip)

    # Opportunistic password-hash upgrade: if the stored hash is the legacy
    # SHA-256 format, rewrite it as bcrypt now that we know the plaintext is
    # correct. Next login uses the bcrypt fast path. No-op if already bcrypt.
    _maybe_upgrade_to_bcrypt(username, password, row[0])

    # Detect the factory-default admin/admin combo. We still issue a session
    # (so the user can call /api/auth/change-password), but flag the session
    # so the server-side middleware refuses every other route until the
    # password is rotated. Belt-and-braces: client also gets the flag.
    must_change = (username == "admin" and _verify_password("admin", row[1], row[0]))

    token = _create_session_token(username, must_change=must_change)
    body_out = {"ok": True, "username": username}
    if must_change:
        body_out["must_change_password"] = True
        body_out["reason"] = "Default credentials detected — set a new password to continue."
    response = JSONResponse(body_out)
    response.set_cookie(
        key="vl_session",
        value=token,
        httponly=True,
        # strict, not lax: single-admin dashboard has no legitimate cross-site
        # entry, so the session cookie is never sent on cross-site requests —
        # a belt-and-suspenders CSRF defense alongside the middleware's
        # Origin check (server.py). Cost: a top-level link from another origin
        # won't carry the cookie, so the user re-navigates once. Acceptable.
        samesite="strict",
        secure=_COOKIE_SECURE,
        max_age=86400,
        path="/",
    )
    return response


@router.post("/logout")
async def logout():
    """Clear session cookie."""
    response = JSONResponse({"ok": True})
    response.delete_cookie("vl_session", path="/")
    return response


@router.post("/change-password")
async def change_password(request: Request):
    """Change the current user's password."""
    # Get current user from cookie
    token = request.cookies.get("vl_session")
    username = validate_session(token)
    if not username:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request"}, status_code=400)

    current_pw = body.get("current_password", "")
    new_pw = body.get("new_password", "")
    new_username = body.get("new_username", "").strip()

    if not current_pw or not new_pw:
        return JSONResponse({"error": "Current and new password required"}, status_code=400)

    # Validate the rename target before it lands in the DB + the new session
    # token. Blocks ':' (token-delimiter corruption) and CRLF/';' (cookie
    # injection) from reaching the Set-Cookie value.
    if new_username and not _USERNAME_RE.match(new_username):
        return JSONResponse(
            {"error": "Username may only contain letters, digits, dot, dash, underscore (max 64)"},
            status_code=400,
        )

    if len(new_pw) < MIN_PASSWORD_LENGTH:
        return JSONResponse(
            {"error": f"Password must be at least {MIN_PASSWORD_LENGTH} characters"},
            status_code=400,
        )

    # Block the dumbest reuse — preventing "admin" as the new password
    # when we're trying to migrate users OFF default credentials.
    if new_pw.lower() == "admin":
        return JSONResponse(
            {"error": "Pick a password other than 'admin'"},
            status_code=400,
        )

    db = _get_db()
    try:
        row = db.execute(
            "SELECT password_hash, salt FROM users WHERE username = ?",
            (username,),
        ).fetchone()

        if not row or not _verify_password(current_pw, row[1], row[0]):
            return JSONResponse({"error": "Current password is incorrect"}, status_code=401)

        # Update password (and optionally username)
        new_salt = secrets.token_hex(16)
        new_hash = _hash_password(new_pw, new_salt)
        target_username = new_username if new_username else username
        # Validate the EXACT value that lands in the DB + signed token + cookie,
        # regardless of source (typed rename OR carried-over session username).
        # Guards ':' (token-format corruption) and CRLF/';' (cookie injection).
        if not _USERNAME_RE.match(target_username):
            return JSONResponse(
                {"error": "Username may only contain letters, digits, dot, dash, underscore (max 64)"},
                status_code=400,
            )
        now = time.time()

        db.execute(
            "UPDATE users SET username = ?, password_hash = ?, salt = ?, updated_at = ? "
            "WHERE username = ?",
            (target_username, new_hash, new_salt, now, username),
        )
        db.commit()
    finally:
        db.close()

    # Issue new session token with updated username. must_change is
    # explicitly False here — the user just rotated, so they're no
    # longer on the default credentials.
    new_token = _create_session_token(target_username, must_change=False)
    response = JSONResponse({"ok": True, "username": target_username})
    response.set_cookie(
        key="vl_session",
        value=new_token,
        httponly=True,
        # strict, not lax: single-admin dashboard has no legitimate cross-site
        # entry, so the session cookie is never sent on cross-site requests —
        # a belt-and-suspenders CSRF defense alongside the middleware's
        # Origin check (server.py). Cost: a top-level link from another origin
        # won't carry the cookie, so the user re-navigates once. Acceptable.
        samesite="strict",
        secure=_COOKIE_SECURE,
        max_age=86400,
        path="/",
    )
    return response


@router.get("/status")
async def auth_status(request: Request):
    """Check if the current session is valid."""
    token = request.cookies.get("vl_session")
    username = validate_session(token)
    if username:
        return {"logged_in": True, "username": username}
    return {"logged_in": False}
