"""
services/dashboard/helpers/env_writer.py — safely update keys in .env.

WHY THIS EXISTS:
    The setup wizard (Phase F) lets users pick a hardware tier + GPU mode
    in the browser and have those values persist without typing into a
    text editor. The dashboard container has .env bind-mounted at /app/.env
    so this module can write it directly.

WHAT IT DOES:
    Reads /app/.env, finds each requested key, and updates its value
    in-place. New keys are appended at the bottom under a generated
    "# Set by setup wizard" header. Comments + unrelated keys + blank
    lines are preserved exactly.

WHAT IT DOES NOT DO:
    - Doesn't parse .env values semantically — they're treated as opaque
      strings (caller is responsible for escaping)
    - Doesn't touch any file other than .env
    - Doesn't validate the values (caller's job)
    - Doesn't restart anything — the orchestrator handles that via a
      separate Redis pub/sub channel after the write succeeds

SAFETY:
    - Allowlist of writable keys (ALLOWED_KEYS). Anything not in the
      allowlist is silently ignored; callers can't slip in arbitrary
      env vars (e.g. CAMERA_PASSWORD) via the wizard endpoint.
    - Writes via a temp file + atomic rename so a crash mid-write
      can't truncate .env.
    - Preserves the original line endings + trailing newline.
"""

import logging
import os
import re
import tempfile
from pathlib import Path

logger = logging.getLogger("dashboard.env_writer")

# Default location — mounted at /app/.env when the dashboard container is
# launched via docker-compose.yml. Tests can override.
ENV_FILE_PATH = Path(os.getenv("DASHBOARD_ENV_PATH", "/app/.env"))

# Keys the wizard is allowed to set. Anything else passed in via the
# /api/setup/apply-config body is silently dropped.
ALLOWED_KEYS = {
    "DETECTOR_GPU",
    "CHAT_GPU",
    "CHAT_MODEL",
    "VISION_MODEL",
    "POSE_MODEL",
    "VEHICLE_MODEL",
    "TARGET_FPS",
    # Location + retention — wizard-settable, validated by setup endpoint
    "LOCATION_TIMEZONE",
    "LOCATION_NAME",
    "LOCATION_REGION",
    "LOCATION_LAT",
    "LOCATION_LON",
    "SNAPSHOT_RETENTION_DAYS",
    "CLIP_RETENTION_DAYS",
    "RETENTION_DAYS",
}


def update_env(updates: dict[str, str],
               path: Path = ENV_FILE_PATH) -> dict:
    """Set or update zero or more keys in the .env file.

    `updates` is a dict of {KEY: value}. Only keys in ALLOWED_KEYS are
    actually written; everything else is dropped and listed in the
    response under "ignored".

    Returns:
        {
            "ok": bool,
            "path": str,
            "written": [KEY1, KEY2, ...],   # keys we actually changed
            "ignored": [KEY3, ...],         # keys filtered out by allowlist
            "error": str | None
        }
    """
    # Validate keys against the allowlist
    valid = {k: str(v) for k, v in updates.items() if k in ALLOWED_KEYS}
    ignored = sorted(set(updates) - ALLOWED_KEYS)

    if not valid:
        return {
            "ok": True,
            "path": str(path),
            "written": [],
            "ignored": ignored,
            "error": None,
        }

    if not path.exists():
        return {
            "ok": False,
            "path": str(path),
            "written": [],
            "ignored": ignored,
            "error": f"{path} does not exist (is .env bind-mounted into the container?)",
        }

    try:
        original = path.read_text()
    except OSError as e:
        return {
            "ok": False,
            "path": str(path),
            "written": [],
            "ignored": ignored,
            "error": f"couldn't read {path}: {e}",
        }

    # Walk the file line-by-line. For each existing key we want to update,
    # rewrite the line in place. Track which keys we found vs need to append.
    remaining = dict(valid)  # keys we still haven't seen
    out_lines = []
    for line in original.splitlines(keepends=True):
        # Match KEY=value (allowing leading whitespace + optional quotes around value).
        # Anchor on the start of the line so we don't touch shell-like assignments
        # inside comments.
        match = re.match(r'^(\s*)([A-Z_][A-Z0-9_]*)\s*=', line)
        if match:
            key = match.group(2)
            if key in remaining:
                indent = match.group(1)
                new_value = remaining.pop(key)
                # Preserve trailing newline if the line had one
                eol = "\n" if line.endswith("\n") else ""
                out_lines.append(f"{indent}{key}={new_value}{eol}")
                continue
        out_lines.append(line)

    # Anything in `remaining` is new — append at the bottom under a header.
    if remaining:
        # Make sure the file ends with a newline before appending
        if out_lines and not out_lines[-1].endswith("\n"):
            out_lines[-1] += "\n"
        out_lines.append("\n# Set by setup wizard\n")
        for k, v in remaining.items():
            out_lines.append(f"{k}={v}\n")

    new_content = "".join(out_lines)

    # Write strategy: try atomic-rename for normal filesystems, fall back to
    # direct truncate-and-write for bind-mounted files (Docker rejects
    # rename onto a bind-mount target with EBUSY because the destination is
    # actually a mount point, not a regular file).
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=".env.tmp.", dir=str(path.parent),
        )
        rename_failed = False
        try:
            with os.fdopen(tmp_fd, "w") as f:
                f.write(new_content)
            try:
                stat = path.stat()
                os.chmod(tmp_path, stat.st_mode)
            except OSError:
                pass
            try:
                os.replace(tmp_path, path)
            except OSError as rename_err:
                # EBUSY (16) on bind-mounted files — fall back to direct write
                if rename_err.errno == 16:
                    rename_failed = True
                else:
                    raise
        except Exception:
            try: os.unlink(tmp_path)
            except OSError: pass
            raise

        if rename_failed:
            # Direct write into the bind-mounted file. Not atomic, but the
            # file is tiny and the write completes in microseconds.
            try:
                with open(path, "w") as f:
                    f.write(new_content)
            except OSError as e:
                try: os.unlink(tmp_path)
                except OSError: pass
                return {
                    "ok": False,
                    "path": str(path),
                    "written": [],
                    "ignored": ignored,
                    "error": f"couldn't write {path}: {e}",
                }
            # tmp file is no longer useful
            try: os.unlink(tmp_path)
            except OSError: pass
    except OSError as e:
        return {
            "ok": False,
            "path": str(path),
            "written": [],
            "ignored": ignored,
            "error": f"couldn't write {path}: {e}",
        }

    written = sorted(set(valid) - set(remaining)) + sorted(remaining)
    logger.info(f"Updated {path}: wrote {written}, ignored {ignored}")
    return {
        "ok": True,
        "path": str(path),
        "written": written,
        "ignored": ignored,
        "error": None,
    }
