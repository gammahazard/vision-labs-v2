"""
services/face-recognizer/recognizer.py — Face recognition and re-identification service.

PURPOSE:
    Identifies known people by matching face embeddings against a SQLite database.
    Also provides a REST API for face enrollment from the dashboard.

RELATIONSHIPS:
    - Reads from: Redis streams (frames, detections) — gets face crops
    - Writes to: Redis stream (identifications:{camera_id}) — enriched detections with names
    - Writes to: Redis hash (identities:{camera_id}) — current person names in frame
    - Uses: face_db.py for SQLite face storage
    - Uses: InsightFace (buffalo_l) for face embedding generation
    - REST API used by: dashboard for enrollment

DATA FLOW:
    frames + detections → THIS SERVICE → crops face from bbox
    → generates embedding → matches against SQLite → publishes name
    Dashboard → POST /enroll → THIS SERVICE → stores in SQLite

ENVIRONMENT VARIABLES:
    CAMERA_ID       — Which camera stream to process (default: cam1)
    REDIS_HOST      — Redis server host
    REDIS_PORT      — Redis server port
    DB_PATH         — Path to SQLite database file (default: /data/faces.db)
    MATCH_THRESHOLD — Cosine similarity threshold for recognition (default: 0.45)
    API_PORT        — REST API port for enrollment (default: 8081)
"""

import base64
import json
import os
import sys
import time
import signal
import logging
import threading

import cv2
import numpy as np
import redis
from contracts.redis_client import make_redis_client
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
import uvicorn

from face_db import FaceDB

# Import stream key definitions from contracts (single source of truth)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "contracts"))
from streams import (
    FRAME_STREAM as _FRAME_TMPL,
    DETECTION_STREAM as _DET_TMPL,
    EVENT_STREAM as _EVT_TMPL,
    IDENTITY_STREAM as _ID_TMPL,
    IDENTITY_KEY as _IDKEY_TMPL,
    stream_key,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CAMERA_ID = os.getenv("CAMERA_ID", "cam1")
REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
DB_PATH = os.getenv("DB_PATH", "/data/faces.db")
MATCH_THRESHOLD = float(os.getenv("MATCH_THRESHOLD", "0.45"))
API_PORT = int(os.getenv("API_PORT", "8081"))
CONSUMER_GROUP = os.getenv("CONSUMER_GROUP", "face_recognizers")
CONSUMER_NAME = os.getenv("CONSUMER_NAME", "recognizer_1")
# How often to re-read the shared SQLite caches. Needed when multiple
# face-recognizer containers (e.g. cam2) share the same DB — without this,
# only the container that handled an enrollment knows about it.
CACHE_REFRESH_INTERVAL = float(os.getenv("CACHE_REFRESH_INTERVAL", "30"))

# Stream keys — resolved from contracts/streams.py
FRAME_STREAM = stream_key(_FRAME_TMPL, camera_id=CAMERA_ID)
DETECTION_STREAM = stream_key(_DET_TMPL, detector_type="pose", camera_id=CAMERA_ID)
IDENTITY_STREAM = stream_key(_ID_TMPL, camera_id=CAMERA_ID)
IDENTITY_KEY = stream_key(_IDKEY_TMPL, camera_id=CAMERA_ID)
EVENT_STREAM = stream_key(_EVT_TMPL, camera_id=CAMERA_ID)

MAX_IDENTITY_STREAM_LEN = 1000

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("face-recognizer")

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    logger.info(f"Received signal {signum}, shutting down...")
    _shutdown = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)

# ---------------------------------------------------------------------------
# InsightFace Model Loading
# ---------------------------------------------------------------------------
_face_analyzer = None


def load_face_model():
    """
    Load the InsightFace model for face detection + embedding.

    Uses the 'buffalo_l' model which provides:
    - Face detection (retinaface)
    - Face alignment
    - Face embedding (arcface, 512-dim vectors)
    """
    global _face_analyzer
    from insightface.app import FaceAnalysis

    logger.info("Loading InsightFace model (buffalo_l)...")
    _face_analyzer = FaceAnalysis(
        name="buffalo_l",
        providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
    )
    # det_size controls face detection input resolution
    _face_analyzer.prepare(ctx_id=0, det_size=(320, 320))
    logger.info("InsightFace model loaded successfully")
    return _face_analyzer


def get_face_embedding(
    frame: np.ndarray, bbox: list
) -> tuple[np.ndarray, bytes, list, float, str | None, float | None] | None:
    """Crop a person bbox and run InsightFace.

    Returns (embedding, jpeg_thumbnail, face_bbox, det_score, sex, age)
    or None if no face was detected. `sex` is 'M'/'F' from buffalo_l's
    genderage head (None if attribute missing); `age` is a float year
    estimate with ~7-year MAE.
    """
    """
    Extract face embedding from a person's bounding box region.

    Steps:
    1. Crop the upper portion of the person bbox (head area)
    2. Run InsightFace detection on the crop
    3. If a face is found, return its 512-dim embedding + JPEG thumbnail

    Args:
        frame: Full camera frame (numpy array)
        bbox: Person bounding box [x1, y1, x2, y2]

    Returns:
        (embedding, jpeg_thumbnail) or None if no face detected
    """
    if _face_analyzer is None:
        return None

    x1, y1, x2, y2 = [int(v) for v in bbox]
    h, w = frame.shape[:2]

    # Clamp to frame bounds
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)

    # Crop the upper half of the person bbox (where the face is)
    person_height = y2 - y1
    face_y2 = y1 + int(person_height * 0.5)  # Upper 50% of person
    face_crop = frame[y1:face_y2, x1:x2]

    if face_crop.size == 0:
        return None

    # Run InsightFace on the crop
    faces = _face_analyzer.get(face_crop)

    if not faces:
        return None

    # Take the largest face detected
    face = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))

    # Quality gate: reject low-quality face detections that produce bad embeddings
    # (these would fail to match and create false unknowns)
    det_score = getattr(face, "det_score", 1.0)
    if det_score < 0.5:
        return None

    # Get the 512-dim embedding
    embedding = face.embedding

    # buffalo_l's genderage head populates these for free during recognition.
    # Wrap in getattr so a future model swap that lacks the head doesn't crash.
    raw_sex = getattr(face, "sex", None)
    if raw_sex is not None:
        raw_sex = str(raw_sex)  # InsightFace returns 'M'/'F' as plain strings
    raw_age = getattr(face, "age", None)
    if raw_age is not None:
        raw_age = float(raw_age)

    # Calculate face bbox in full-frame coordinates (for dashboard overlay)
    fb = face.bbox.astype(int)
    face_bbox = [
        int(x1 + max(0, fb[0])),
        int(y1 + max(0, fb[1])),
        int(x1 + min(face_crop.shape[1], fb[2])),
        int(y1 + min(face_crop.shape[0], fb[3])),
    ]

    # Create a JPEG thumbnail of the face for the dashboard
    # Use full-frame coordinates with generous padding for a natural portrait
    abs_fx1 = x1 + max(0, fb[0])
    abs_fy1 = y1 + max(0, fb[1])
    abs_fx2 = x1 + min(face_crop.shape[1], fb[2])
    abs_fy2 = y1 + min(face_crop.shape[0], fb[3])
    abs_fw = abs_fx2 - abs_fx1
    abs_fh = abs_fy2 - abs_fy1
    pad_x = int(abs_fw * 1.2)   # 120% horizontal padding — shows shoulders
    pad_y = int(abs_fh * 1.0)   # 100% vertical padding — shows hair + neck
    fx1 = max(0, abs_fx1 - pad_x)
    fy1 = max(0, abs_fy1 - pad_y)
    fx2 = min(w, abs_fx2 + pad_x)
    fy2 = min(h, abs_fy2 + int(pad_y * 1.5))  # Extra below for shoulders
    face_thumb = frame[fy1:fy2, fx1:fx2]  # Crop from FULL frame, not face_crop

    if face_thumb.size == 0:
        face_thumb = face_crop

    # Resize thumbnail to standard size (larger for better quality)
    face_thumb = cv2.resize(face_thumb, (200, 200))
    _, jpg_buf = cv2.imencode(".jpg", face_thumb, [cv2.IMWRITE_JPEG_QUALITY, 90])
    jpeg_bytes = jpg_buf.tobytes()

    return embedding, jpeg_bytes, face_bbox, det_score, raw_sex, raw_age


# ---------------------------------------------------------------------------
# Redis Consumer Group Setup
# ---------------------------------------------------------------------------
def setup_consumer_group(r: redis.Redis):
    """Create consumer group for the detection stream."""
    try:
        r.xgroup_create(DETECTION_STREAM, CONSUMER_GROUP, id="$", mkstream=True)
        logger.info(f"Created consumer group '{CONSUMER_GROUP}' on {DETECTION_STREAM}")
    except redis.ResponseError as e:
        if "BUSYGROUP" in str(e):
            logger.info(f"Consumer group '{CONSUMER_GROUP}' already exists")
        else:
            raise


# ---------------------------------------------------------------------------
# REST API for enrollment (runs in separate thread)
# ---------------------------------------------------------------------------
face_db: FaceDB = None
r_global: redis.Redis = None
# Module-level binary client reused by the enrollment + preview routes.
# Avoids constructing a fresh connection pool per HTTP request.
r_bin_global: redis.Redis = None

api = FastAPI(title="Face Recognizer API")


@api.get("/api/faces")
async def list_faces():
    """List all enrolled faces."""
    return {"faces": face_db.list_faces(), "count": face_db.count}


@api.get("/api/faces/{face_id}/photo")
async def get_face_photo(face_id: int):
    """Get the JPEG thumbnail for an enrolled face."""
    photo = face_db.get_photo(face_id)
    if photo:
        return Response(content=photo, media_type="image/jpeg")
    return JSONResponse(status_code=404, content={"error": "Face not found"})


@api.post("/api/faces/preview")
async def preview_face():
    """
    Preview the face that would be enrolled from the current camera frame.

    Returns a base64 JPEG thumbnail of the detected face WITHOUT enrolling.
    The dashboard shows this to the user for confirmation before enrolling.
    """
    r_bin = r_bin_global

    # Get the latest frame
    frames = r_bin.xrevrange(FRAME_STREAM, count=1)
    if not frames:
        return JSONResponse(status_code=404, content={"error": "No frames available"})

    frame_bytes = frames[0][1][b"frame"]
    np_arr = np.frombuffer(frame_bytes, np.uint8)
    frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)

    if frame is None:
        return JSONResponse(status_code=500, content={"error": "Failed to decode frame"})

    # Get latest detections
    detections_raw = r_bin.xrevrange(
        DETECTION_STREAM.encode(), count=1
    )
    if not detections_raw:
        return JSONResponse(status_code=404, content={"error": "No detections available"})

    det_json = detections_raw[0][1].get(b"detections", b"[]").decode()
    detections = json.loads(det_json)

    if not detections:
        return JSONResponse(
            status_code=404,
            content={"error": "No person detected in current frame"},
        )

    # Find the largest person (most likely the one enrolling)
    largest = max(
        detections,
        key=lambda d: (d["bbox"][2] - d["bbox"][0]) * (d["bbox"][3] - d["bbox"][1]),
    )

    # Extract face embedding + thumbnail
    result = get_face_embedding(frame, largest["bbox"])
    if result is None:
        return JSONResponse(
            status_code=404,
            content={"error": "No face detected — try facing the camera directly"},
        )

    _, photo, _, _, sex, age = result
    photo_b64 = base64.b64encode(photo).decode("ascii")

    return {
        "success": True,
        "preview": photo_b64,
        "bbox": largest["bbox"],
        "num_people": len(detections),
        "sex": sex,
        "age": age,
    }


@api.post("/api/faces/enroll")
async def enroll_face(data: dict):
    """
    Enroll a new face from the current camera frame.

    The dashboard calls this with a person's name. We:
    1. Grab the latest frame + detections from Redis
    2. Find the largest detected person
    3. Extract their face embedding
    4. Store in SQLite with the provided name
    """
    name = data.get("name", "").strip()
    if not name:
        return JSONResponse(status_code=400, content={"error": "Name is required"})

    r_bin = r_bin_global

    # Get the latest frame
    frames = r_bin.xrevrange(FRAME_STREAM, count=1)
    if not frames:
        return JSONResponse(status_code=404, content={"error": "No frames available"})

    frame_bytes = frames[0][1][b"frame"]
    np_arr = np.frombuffer(frame_bytes, np.uint8)
    frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)

    if frame is None:
        return JSONResponse(status_code=500, content={"error": "Failed to decode frame"})

    # Get latest detections
    detections_raw = r_bin.xrevrange(
        DETECTION_STREAM.encode(), count=1
    )
    if not detections_raw:
        return JSONResponse(status_code=404, content={"error": "No detections available"})

    det_json = detections_raw[0][1].get(b"detections", b"[]").decode()
    detections = json.loads(det_json)

    if not detections:
        return JSONResponse(
            status_code=404,
            content={"error": "No person detected in current frame"},
        )

    # Find the largest person (most likely the one enrolling)
    largest = max(
        detections,
        key=lambda d: (d["bbox"][2] - d["bbox"][0]) * (d["bbox"][3] - d["bbox"][1]),
    )

    # Extract face embedding
    result = get_face_embedding(frame, largest["bbox"])
    if result is None:
        return JSONResponse(
            status_code=404,
            content={"error": "No face detected — try facing the camera directly"},
        )

    embedding, photo, _, _, sex, age = result
    face_id = face_db.enroll(name, embedding, photo, sex=sex, age=age)

    # Retroactively clear unknowns that match this newly enrolled face
    absorb_result = face_db.match_and_clear_unknowns(name, embedding)
    cleared = absorb_result["count"]
    promoted = absorb_result["promoted"]

    # Publish enrollment event to event stream
    try:
        r_global.xadd(EVENT_STREAM, {
            "event_type": "face_enrolled",
            "person_id": name,
            "timestamp": str(time.time()),
            "action": f"Enrolled with face_id={face_id}",
            "duration": "0",
            "direction": "",
            "camera_id": CAMERA_ID,
        }, maxlen=1000)
        if cleared > 0:
            r_global.xadd(EVENT_STREAM, {
                "event_type": "face_reconciled",
                "person_id": name,
                "timestamp": str(time.time()),
                "action": f"Absorbed {cleared} unknown(s) as extra angles for {name}",
                "duration": "0",
                "direction": "",
                "camera_id": CAMERA_ID,
                # Per-row details so the dashboard modal can render thumbnails
                # + similarity scores when this event is clicked.
                "promoted_face_ids": json.dumps([p["face_id"] for p in promoted]),
                "similarities": json.dumps([p["similarity"] for p in promoted]),
                "count": str(cleared),
            }, maxlen=1000)
    except Exception as e:
        logger.warning(f"Failed to publish enrollment event: {e}")

    return {
        "success": True,
        "face_id": face_id,
        "name": name,
        "message": f"Enrolled {name} successfully!",
        "unknowns_cleared": cleared,
    }


@api.delete("/api/faces/{face_id}")
async def delete_face(face_id: int):
    """Remove an enrolled face."""
    deleted = face_db.delete(face_id)
    if deleted:
        return {"success": True, "message": f"Face {face_id} deleted"}
    return JSONResponse(status_code=404, content={"error": "Face not found"})


@api.post("/api/faces/by_name/{name}/demographics")
async def set_face_demographics(name: str, data: dict):
    """Pin gender + age for every angle of `name`.

    Overrides the model's genderage estimate when it whiffs (common at
    the extremes — kids and seniors). Pass `null`/missing for either
    field to revert to the model's value.
    """
    sex = data.get("sex")
    age = data.get("age")
    if sex is not None and sex not in ("M", "F"):
        return JSONResponse(
            status_code=400,
            content={"error": "sex must be 'M', 'F', or null"},
        )
    if age is not None:
        try:
            age = float(age)
        except (TypeError, ValueError):
            return JSONResponse(
                status_code=400, content={"error": "age must be a number"}
            )
        if age < 0 or age > 150:
            return JSONResponse(
                status_code=400, content={"error": "age must be 0-150"}
            )
    count = face_db.set_demographics_override(name, sex, age)
    if count == 0:
        return JSONResponse(
            status_code=404, content={"error": f"No faces enrolled under '{name}'"}
        )
    return {"success": True, "updated": count, "sex": sex, "age": age}


# ---------------------------------------------------------------------------
# Unknown faces API — auto-captured faces for retroactive labeling
# ---------------------------------------------------------------------------

@api.get("/api/unknowns")
async def list_unknowns():
    """List all auto-captured unknown faces."""
    return {"unknowns": face_db.list_unknowns(), "count": face_db.unknown_count}


@api.get("/api/unknowns/{uid}/photo")
async def get_unknown_photo(uid: int):
    """Get the JPEG thumbnail for an unknown face."""
    photo = face_db.get_unknown_photo(uid)
    if photo:
        return Response(content=photo, media_type="image/jpeg")
    return JSONResponse(status_code=404, content={"error": "Unknown face not found"})


@api.post("/api/unknowns/{uid}/label")
async def label_unknown(uid: int, data: dict):
    """Promote an unknown face to known by assigning a name."""
    name = data.get("name", "").strip()
    if not name:
        return JSONResponse(status_code=400, content={"error": "Name is required"})

    face_id = face_db.label_unknown(uid, name)
    if face_id is None:
        return JSONResponse(status_code=404, content={"error": "Unknown face not found"})

    # Retroactively clear other unknowns that match this newly labeled face
    # Get the embedding from the cache for retroactive matching
    labeled_embedding = None
    for cached in face_db._cache:
        if cached["id"] == face_id:
            labeled_embedding = cached["embedding"]
            break

    cleared = 0
    promoted: list[dict] = []
    if labeled_embedding is not None:
        absorb_result = face_db.match_and_clear_unknowns(name, labeled_embedding)
        cleared = absorb_result["count"]
        promoted = absorb_result["promoted"]

    # Publish labeling event to event stream
    try:
        r_global.xadd(EVENT_STREAM, {
            "event_type": "face_enrolled",
            "person_id": name,
            "timestamp": str(time.time()),
            "action": f"Labeled from unknown #{uid}",
            "duration": "0",
            "direction": "",
            "camera_id": CAMERA_ID,
        }, maxlen=1000)
        if cleared > 0:
            r_global.xadd(EVENT_STREAM, {
                "event_type": "face_reconciled",
                "person_id": name,
                "timestamp": str(time.time()),
                "action": f"Absorbed {cleared} unknown(s) as extra angles for {name}",
                "duration": "0",
                "direction": "",
                "camera_id": CAMERA_ID,
                "promoted_face_ids": json.dumps([p["face_id"] for p in promoted]),
                "similarities": json.dumps([p["similarity"] for p in promoted]),
                "count": str(cleared),
            }, maxlen=1000)
    except Exception as e:
        logger.warning(f"Failed to publish label event: {e}")

    return {
        "success": True,
        "face_id": face_id,
        "name": name,
        "message": f"Labeled as {name} — will be recognized from now on!",
        "unknowns_cleared": cleared,
    }


@api.delete("/api/unknowns/{uid}")
async def delete_unknown(uid: int):
    """Remove an auto-captured unknown face."""
    deleted = face_db.delete_unknown(uid)
    if deleted:
        return {"success": True, "message": f"Unknown {uid} deleted"}
    return JSONResponse(status_code=404, content={"error": "Unknown face not found"})


@api.post("/api/unknowns/scan")
async def scan_unknowns():
    """
    Manually trigger the reconcile sweep — promote unknowns that match
    enrolled people (as extra angles) and delete loose matches. Same
    logic as startup reconcile, exposed for dashboard "Scan unknowns".

    Useful workflow: label a few unknowns to bootstrap recognition for
    a person, then hit Scan to vacuum up the rest of their gallery
    rows in one shot.
    """
    before = face_db.unknown_count
    result = face_db.reconcile_unknowns()
    after = face_db.unknown_count

    # Publish a per-person face_reconciled event for the feed. Include
    # the per-row promotion details so the modal can show thumbnails.
    matched_names = result.get("matched_names", {})
    promoted_by_name = result.get("promoted_by_name", {})
    for name, cnt in matched_names.items():
        promoted = promoted_by_name.get(name, [])
        try:
            r_global.xadd(EVENT_STREAM, {
                "event_type": "face_reconciled",
                "person_id": name,
                "timestamp": str(time.time()),
                "action": f"Manual scan: reconciled {cnt} unknown(s) against {name}",
                "duration": "0",
                "direction": "",
                "camera_id": CAMERA_ID,
                "promoted_face_ids": json.dumps([p["face_id"] for p in promoted]),
                "similarities": json.dumps([p["similarity"] for p in promoted]),
                "count": str(cnt),
            }, maxlen=1000)
        except Exception as e:
            logger.warning(f"Failed to publish scan event: {e}")

    return {
        "success": True,
        "promoted": result.get("promoted", 0),
        "deleted": result.get("deleted", 0),
        "matched_names": matched_names,
        "before": before,
        "after": after,
        "message": (
            f"Promoted {result.get('promoted', 0)} angle(s) and "
            f"deleted {result.get('deleted', 0)} loose match(es). "
            f"{after} unknown(s) remaining."
        ),
    }


@api.delete("/api/unknowns")
async def clear_all_unknowns():
    """Remove all auto-captured unknown faces."""
    count = face_db.unknown_count
    if count == 0:
        return {"success": True, "cleared": 0, "message": "No unknowns to clear"}

    import sqlite3 as _sql
    with _sql.connect(face_db.db_path) as conn:
        conn.execute("DELETE FROM unknown_faces")
        conn.commit()
    face_db._unknown_cache.clear()
    logger.info(f"Cleared all {count} unknown faces")
    return {"success": True, "cleared": count, "message": f"Cleared {count} unknown faces"}


def start_api():
    """Run the FastAPI enrollment API in a separate thread."""
    uvicorn.run(api, host="0.0.0.0", port=API_PORT, log_level="warning")


def cache_refresh_loop():
    """Periodically reload face_db caches from SQLite.

    Required for multi-container deployments where one face-recognizer
    handles enrollments (cam1, the one wired to the dashboard) and
    other instances (e.g. cam2) need to pick up new known faces or
    unknown captures without a restart.

    The reload is an atomic swap (see FaceDB._load_cache), so live
    matching on the main thread keeps working without lock contention.
    """
    while not _shutdown:
        # Sleep in short chunks so SIGTERM is honored quickly
        slept = 0.0
        while slept < CACHE_REFRESH_INTERVAL and not _shutdown:
            time.sleep(min(1.0, CACHE_REFRESH_INTERVAL - slept))
            slept += 1.0
        if _shutdown:
            break
        try:
            face_db._load_cache()
        except Exception as e:
            logger.warning(f"Periodic cache refresh failed: {e}")


# ---------------------------------------------------------------------------
# Main Recognition Loop
# ---------------------------------------------------------------------------
def _check_camera_wants_detector(r, detector_flag: str) -> bool:
    """Phase 7c: respect camera-registry detect_<type> flag; True if absent or unreachable."""
    try:
        import json as _json
        raw = r.hget("cameras:registry", CAMERA_ID)
        if not raw:
            return True
        entry = _json.loads(raw if isinstance(raw, str) else raw.decode())
        return bool(entry.get(detector_flag, True))
    except Exception:
        return True


def run():
    """
    Main loop: read detections → crop faces → match against known faces → publish.

    For each detection:
    1. Get the original frame from Redis
    2. Crop the face region from the person's bounding box
    3. Generate a 512-dim face embedding
    4. Compare against all known faces in SQLite
    5. If match found → publish enriched detection with person's name
    """
    global face_db, r_global, r_bin_global

    # Initialize face database
    face_db = FaceDB(db_path=DB_PATH, match_threshold=MATCH_THRESHOLD)
    logger.info(f"Face database: {face_db.count} known faces, {face_db.unknown_count} unknowns loaded")

    # Load InsightFace model
    load_face_model()

    # Connect to Redis
    r = make_redis_client(decode_responses=False, host=REDIS_HOST, port=REDIS_PORT)
    r.ping()
    r_global = r
    # Shared binary client for the REST handlers (preview/enroll).
    r_bin_global = r
    logger.info("Redis connection verified")

    # Phase 7c: skip this service if the camera registry says faces aren't wanted.
    r_text = make_redis_client(decode_responses=True, host=REDIS_HOST, port=REDIS_PORT)
    if not _check_camera_wants_detector(r_text, "detect_faces"):
        logger.info(f"Camera '{CAMERA_ID}' has detect_faces=false — exiting cleanly")
        return

    # Reconcile: promote strong matches as extra angles, delete loose ones
    result = face_db.reconcile_unknowns()
    matched_names = result.get("matched_names", {})
    promoted_by_name = result.get("promoted_by_name", {})
    if matched_names:
        total = sum(matched_names.values())
        detail = ", ".join(f"{name}: {cnt}" for name, cnt in matched_names.items())
        logger.info(f"Startup reconciliation: processed {total} unknowns ({detail})")
        try:
            for name, cnt in matched_names.items():
                promoted = promoted_by_name.get(name, [])
                r.xadd(EVENT_STREAM, {
                    "event_type": "face_reconciled",
                    "person_id": name,
                    "timestamp": str(time.time()),
                    "action": f"Startup: reconciled {cnt} unknown(s) against {name}",
                    "duration": "0",
                    "direction": "",
                    "camera_id": CAMERA_ID,
                    "promoted_face_ids": json.dumps([p["face_id"] for p in promoted]),
                    "similarities": json.dumps([p["similarity"] for p in promoted]),
                    "count": str(cnt),
                }, maxlen=1000)
        except Exception as e:
            logger.warning(f"Failed to publish reconcile event: {e}")

    # Setup consumer group
    setup_consumer_group(r)

    # Start enrollment API in background thread
    api_thread = threading.Thread(target=start_api, daemon=True)
    api_thread.start()
    logger.info(f"Enrollment API running on port {API_PORT}")

    # Periodic cache refresh — keeps multi-container deployments in sync.
    # The dashboard only proxies enrollments to ONE recognizer (cam1),
    # so without this, every other recognizer instance would have a stale
    # cache until restart.
    cache_thread = threading.Thread(target=cache_refresh_loop, daemon=True)
    cache_thread.start()
    logger.info(
        f"Cache refresh loop running (interval={CACHE_REFRESH_INTERVAL}s)"
    )

    # Tracking metrics
    frames_processed = 0
    faces_matched = 0
    last_log_time = time.time()

    while not _shutdown:
        try:
            messages = r.xreadgroup(
                CONSUMER_GROUP,
                CONSUMER_NAME,
                {DETECTION_STREAM: ">"},
                count=1,
                block=1000,
            )
        except redis.ConnectionError:
            logger.warning("Redis connection lost — retrying...")
            time.sleep(1)
            continue

        if not messages:
            continue

        for stream_name, entries in messages:
            for message_id, data in entries:
                timestamp = data.get(b"timestamp", b"0").decode()
                frame_number = data.get(b"frame_number", b"0").decode()
                det_json = data.get(b"detections", b"[]").decode()
                detections = json.loads(det_json)

                # Skip if no detections — but FIRST clear identity_state so the
                # dashboard doesn't keep showing the last known face after the
                # scene has emptied. (Don't clear when there ARE people but no
                # faces matched; that's a separate sticky-identity case the
                # tracker handles.)
                if not detections:
                    try:
                        r.delete(IDENTITY_KEY)
                    except Exception:
                        pass
                    r.xack(DETECTION_STREAM, CONSUMER_GROUP, message_id)
                    frames_processed += 1
                    continue

                # Get the matching frame for face cropping
                frames = r.xrevrange(FRAME_STREAM, count=1)
                if not frames:
                    r.xack(DETECTION_STREAM, CONSUMER_GROUP, message_id)
                    continue

                frame_bytes = frames[0][1][b"frame"]
                np_arr = np.frombuffer(frame_bytes, np.uint8)
                frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)

                if frame is None:
                    r.xack(DETECTION_STREAM, CONSUMER_GROUP, message_id)
                    continue

                # Process each detection (individually wrapped for fault tolerance)
                identities = []
                for det in detections:
                    try:
                        bbox = det.get("bbox", [])
                        if len(bbox) != 4:
                            continue

                        result = get_face_embedding(frame, bbox)
                        if result is None:
                            identities.append({
                                "bbox": bbox,
                                "name": "Unknown",
                                "similarity": 0,
                            })
                            continue

                        embedding, photo, face_bbox, det_score, sex, age = result
                        match = face_db.match(embedding)

                        if match:
                            # Manual override (if set) wins over the live
                            # model output — kids and seniors are the usual
                            # cases where users pin a value.
                            disp_sex = match.get("sex_override") or sex
                            disp_age = (match.get("age_override")
                                        if match.get("age_override") is not None
                                        else age)
                            identities.append({
                                "bbox": bbox,
                                "face_bbox": face_bbox,
                                "name": match["name"],
                                "similarity": match["similarity"],
                                "sex": disp_sex,
                                "age": disp_age,
                            })
                            faces_matched += 1
                        else:
                            identities.append({
                                "bbox": bbox,
                                "face_bbox": face_bbox,
                                "name": "Unknown",
                                "similarity": 0,
                                "sex": sex,
                                "age": age,
                            })
                            # Only save unknowns if face detection is decent quality
                            if det_score >= 0.75:
                                face_db.save_unknown(embedding, photo, sex=sex, age=age)
                    except Exception as e:
                        logger.warning(f"Error processing detection {det}: {e}")
                        continue

                # Publish identities to Redis
                if identities:
                    identity_data = {
                        "camera_id": CAMERA_ID,
                        "timestamp": timestamp,
                        "frame_number": frame_number,
                        "identities": json.dumps(identities),
                        "num_identified": str(
                            sum(1 for i in identities if i["name"] != "Unknown")
                        ),
                    }
                    r.xadd(
                        IDENTITY_STREAM,
                        {k.encode(): v.encode() for k, v in identity_data.items()},
                        maxlen=MAX_IDENTITY_STREAM_LEN,
                    )

                    # Update current identity state (for dashboard overlay).
                    # TTL of 5 minutes so a crashed/down face-recognizer can't
                    # keep a stale identity hash visible to the tracker
                    # indefinitely — tracker reads this hash on a 2 s poll, so
                    # 300 s is generous head-room while still self-cleaning if
                    # this container dies.
                    r.hset(
                        IDENTITY_KEY,
                        mapping={k.encode(): v.encode() for k, v in identity_data.items()},
                    )
                    r.expire(IDENTITY_KEY, 300)

                # Acknowledge
                r.xack(DETECTION_STREAM, CONSUMER_GROUP, message_id)
                frames_processed += 1

                # Log progress every 10 seconds
                now = time.time()
                if now - last_log_time >= 10:
                    logger.info(
                        f"Processed {frames_processed} frames | "
                        f"Matched {faces_matched} faces | "
                        f"Known faces: {face_db.count}"
                    )
                    last_log_time = now

    logger.info(f"Face recognizer stopped. Processed: {frames_processed}, Matched: {faces_matched}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    run()
