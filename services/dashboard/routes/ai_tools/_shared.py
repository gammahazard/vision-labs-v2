"""
routes/ai_tools/_shared.py — constants + helpers shared by every AI tool.

PURPOSE:
    Single source of truth for things every tool needs:
      - KNOWN_EVENT_TYPES — the canonical list (used in schema docs + zero-
        count seeding in aggregation tools)
      - EVENT_CATEGORIES — semantic filter buckets ("people", "vehicles", …)
      - _category_matches — filter helper
      - _resolve_camera, _camera_key, _camera_name, _get_camera_list —
        multi-camera helpers that wrap the registry resolver in cameras.py
      - _redact_sensitive — strip RTSP creds / tokens before any tool
        result reaches the LLM

WHY THIS MODULE EXISTS:
    Before the ai_tools split, all 2000+ lines lived in one file. Each
    individual tool module now imports from here so the constants stay
    consistent and the multi-camera resolver logic isn't duplicated.
"""

import logging

import routes as ctx

logger = logging.getLogger("dashboard.ai")

# Timezone — validated single source of truth (see contracts/tz.py).
from contracts.tz import TZ_LOCAL  # noqa: F401  (re-exported)


# Every event_type string emitted somewhere in the pipeline. Used so type-
# aggregating tools can pre-populate zero counts — the LLM should never
# have to guess whether "no entry for vehicle_idle" means "zero occurred"
# vs "type doesn't exist."
KNOWN_EVENT_TYPES = (
    "person_appeared",
    "person_left",
    "person_identified",
    "vehicle_detected",
    "vehicle_left",
    "vehicle_idle",
    "face_enrolled",
    "face_reconciled",
    "action_changed",
    "unauthorized_access",
    # Stream-health events emitted by camera-ingester when frames stop/resume.
    "stream_stale",
    "stream_recovered",
    # Recorder health events emitted by recorder when ffmpeg keeps crashing.
    "recorder_error",
    "recorder_recovered",
)
KNOWN_EVENT_TYPES_DOC = ", ".join(KNOWN_EVENT_TYPES)

# Event categories — semantic buckets the LLM can filter by without needing
# to know every event_type string.
EVENT_CATEGORIES = {
    "people": ("person_appeared", "person_left", "person_identified"),
    "vehicles": ("vehicle_detected", "vehicle_left", "vehicle_idle"),
    "faces": ("face_enrolled", "face_reconciled", "person_identified"),
    "actions": ("action_changed",),
    "security": ("unauthorized_access",),
    "system": ("stream_stale", "stream_recovered", "recorder_error", "recorder_recovered"),
    "all": KNOWN_EVENT_TYPES,
}


def _category_matches(event_type: str, category: str) -> bool:
    """Check if an event_type belongs to a category. Empty/all => match everything."""
    if not category or category == "all":
        return True
    allowed = EVENT_CATEGORIES.get(category)
    if allowed is None:
        return True  # unknown category — don't filter (fail-open)
    return event_type in allowed


# ---------------------------------------------------------------------------
# Multi-camera helpers (Phase 9a)
# ---------------------------------------------------------------------------
# Tools that take a `camera` arg delegate to the shared resolver in cameras.py
# so registry semantics stay consistent across AI tools, Telegram, and routes.
#
# Conventions for the `camera` tool arg:
#   "all"          -> every registered camera, aggregated
#   "<id>"         -> just that one camera (must exist in cameras:registry)
#   missing/empty  -> the dashboard's primary camera (ctx.CAMERA_ID env)
import cameras as _camreg


def _get_camera_list() -> list:
    """Return all enabled cameras from the registry."""
    return _camreg.list_enabled_cameras()


def _resolve_camera(arg: str = "") -> list:
    """Resolve a tool's `camera` arg into a concrete list of camera ids.
    Returns [] iff a specific id was passed but doesn't exist."""
    return _camreg.resolve_camera_arg(arg, ctx.CAMERA_ID)


def _camera_key(template: str, camera_id: str, **extra) -> str:
    """Build a Redis key for any camera using contracts/streams.py templates.
    Lazy import to avoid circular issues at module load time."""
    from contracts.streams import stream_key
    return stream_key(template, camera_id=camera_id, **extra)


def _camera_name(camera_id: str) -> str:
    """Look up a camera's display name (falls back to id)."""
    return _camreg.camera_friendly_name(camera_id)


# Hash keys that should never end up in an LLM tool result. Anything
# matching one of these substrings (case-insensitive) gets replaced with
# "[redacted]" before the dict is JSON-serialized.
_REDACT_KEY_FRAGMENTS = ("password", "token", "secret", "rtsp", "url",
                         "api_key", "credential")


def _redact_sensitive(d: "dict | None") -> dict:
    """Strip sensitive values from a Redis hash before showing to the LLM.

    The LLM's tool context flows into its reply, and the reply flows to
    Telegram, the browser, and chat history. We don't want RTSP URLs
    with `user:pass@` baked into any of those surfaces.
    """
    if not d:
        return {}
    out: dict = {}
    for k, v in d.items():
        kl = str(k).lower()
        if any(frag in kl for frag in _REDACT_KEY_FRAGMENTS):
            out[k] = "[redacted]"
        else:
            out[k] = v
    return out
