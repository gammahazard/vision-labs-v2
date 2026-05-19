"""
routes/ai_state.py — Shared state for the AI assistant module.

PURPOSE:
    Holds module-level globals and setter functions used by ai.py,
    ai_tools.py, and ai_prompts.py. Centralizes state to avoid
    circular imports.
"""

# ---------------------------------------------------------------------------
# Shared state — set by server.py via setter functions
# ---------------------------------------------------------------------------
_ai_db = None
_model_gpu_ready = False  # Set True by server.py after warm-up chat succeeds


def set_ai_db(db):
    """Called by server.py to inject the AI database instance."""
    global _ai_db
    _ai_db = db




def set_gpu_ready_flag(ready: bool):
    """Called by server.py once the warm-up chat confirms model is in GPU memory."""
    global _model_gpu_ready
    _model_gpu_ready = ready


# ---------------------------------------------------------------------------
# Snapshot/clip side-channel (per-request to avoid race conditions)
# ---------------------------------------------------------------------------
# Base64 images are WAY too large to send back to the LLM as a tool result
# (a 51 KB JPEG = ~68 KB base64 ≈ 53 000 tokens, vs 8 192 context limit).
# Instead we stash them in a per-request dict and the chat handler injects
# the media into the final reply before returning it to the caller.
#
# Why per-request: web chat and Telegram /ask can run concurrently.
# Previously we tracked the "current" request id in a plain module-level
# global, which made the two concurrent paths clobber each other —
# request B would overwrite request A's pointer before A's tool dispatcher
# ran, and A's stashed snapshot would land in B's bucket. The fix is a
# ContextVar: each asyncio task and each thread inherits its own copy,
# so set_request_id() in one chat handler never leaks into another's.
import contextvars
import threading

_media_lock = threading.Lock()
_pending_media: dict[str, dict] = {}  # {request_id: {snapshot, clip, images}}

# Per-task / per-thread current request ID. Inherited by sub-tasks
# automatically (Python contextvars semantics).
_current_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ai_current_request_id", default=None
)


def set_request_id(request_id: str):
    """Set the current request ID for media stashing.

    Returns the previous token so the caller can `reset()` it on exit
    if they want fully balanced scoping, but most callers can ignore
    the return — the ContextVar dies with the task.
    """
    token = _current_request_id.set(request_id)
    with _media_lock:
        _pending_media[request_id] = {"snapshot": None, "clip": None, "images": None}
    return token


def _stash(field: str, value) -> None:
    """Internal: stash a value into the current request's media bucket."""
    rid = _current_request_id.get()
    if rid is None:
        return
    with _media_lock:
        if rid in _pending_media:
            _pending_media[rid][field] = value


def stash_snapshot(b64: str):
    """Stash a base64 snapshot for the current request."""
    _stash("snapshot", b64)


def stash_clip(filename: str):
    """Stash a clip filename for the current request."""
    _stash("clip", filename)


def stash_images(images: list[dict]):
    """Stash browse images for the current request."""
    _stash("images", images)


def collect_media(request_id: str) -> dict:
    """Collect and remove all pending media for a request. Returns {snapshot, clip, images}."""
    with _media_lock:
        return _pending_media.pop(request_id, {"snapshot": None, "clip": None, "images": None})

