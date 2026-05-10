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
# With globals, one request could steal another's media.
import threading

_media_lock = threading.Lock()
_pending_media: dict[str, dict] = {}  # {request_id: {snapshot, clip, images}}

# Current request ID — set by the chat handler before calling the LLM
_current_request_id: str | None = None


def set_request_id(request_id: str):
    """Set the current request ID for media stashing."""
    global _current_request_id
    _current_request_id = request_id
    with _media_lock:
        _pending_media[request_id] = {"snapshot": None, "clip": None, "images": None}


def stash_snapshot(b64: str):
    """Stash a base64 snapshot for the current request."""
    rid = _current_request_id
    if rid:
        with _media_lock:
            if rid in _pending_media:
                _pending_media[rid]["snapshot"] = b64


def stash_clip(filename: str):
    """Stash a clip filename for the current request."""
    rid = _current_request_id
    if rid:
        with _media_lock:
            if rid in _pending_media:
                _pending_media[rid]["clip"] = filename


def stash_images(images: list[dict]):
    """Stash browse images for the current request."""
    rid = _current_request_id
    if rid:
        with _media_lock:
            if rid in _pending_media:
                _pending_media[rid]["images"] = images


def collect_media(request_id: str) -> dict:
    """Collect and remove all pending media for a request. Returns {snapshot, clip, images}."""
    with _media_lock:
        return _pending_media.pop(request_id, {"snapshot": None, "clip": None, "images": None})

