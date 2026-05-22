"""
tests/test_vehicles.py — Real tests for vehicle detection pipeline.

Tests the full vehicle flow: tracker event emission, dashboard event API
returning vehicle-specific fields, browse API for day/snapshot listing,
and path traversal protection.

NO real Redis or GPU — tracker logic and routes are tested with FakeRedis.
"""

import json
import os
import sys
import time
import pytest

# ---------------------------------------------------------------------------
# Path setup — mirror the service's import structure
# ---------------------------------------------------------------------------
_DASHBOARD_DIR = os.path.join(
    os.path.dirname(__file__), "..", "services", "dashboard"
)
_TRACKER_DIR = os.path.join(
    os.path.dirname(__file__), "..", "services", "tracker"
)
_CONTRACTS_DIR = os.path.join(os.path.dirname(__file__), "..", "contracts")
sys.path.insert(0, _DASHBOARD_DIR)
sys.path.insert(0, _TRACKER_DIR)
sys.path.insert(0, _CONTRACTS_DIR)


# ---------------------------------------------------------------------------
# FakeRedis — same mock used in test_routes.py
# ---------------------------------------------------------------------------
class FakeRedis:
    """Minimal Redis mock supporting hash, stream, key, and setex operations."""

    def __init__(self):
        self._hashes = {}   # key → {field: value}
        self._streams = {}  # key → [(id, data)]
        self._keys = {}     # key → value (for GET/SETEX)

    # --- Hash ops ---
    def hset(self, name, key=None, value=None, mapping=None):
        if name not in self._hashes:
            self._hashes[name] = {}
        if mapping:
            for k, v in mapping.items():
                self._hashes[name][k] = str(v) if not isinstance(v, str) else v
        elif key is not None:
            self._hashes[name][key] = value
        return 1

    def hget(self, name, key):
        return self._hashes.get(name, {}).get(key)

    def hgetall(self, name):
        return dict(self._hashes.get(name, {}))

    def hdel(self, name, key):
        if name in self._hashes and key in self._hashes[name]:
            del self._hashes[name][key]
            return 1
        return 0

    # --- Stream ops ---
    def xrevrange(self, name, max=None, min=None, count=None, **kwargs):
        """Mimic real Redis: `max=` and `min=` are stream ID cursors.

        `(ID` (parenthesis prefix) = exclusive bound. `+` / `-` = +∞ / -∞.
        Order: newest first. `count` caps the result length.
        Required for the events route's batched-overscan loop — without
        cursor honoring, the loop would repeatedly read the same batch.
        """
        stream = self._streams.get(name, [])
        reversed_stream = list(reversed(stream))

        def _id_key(sid: str) -> tuple[int, int]:
            try:
                ms, seq = sid.split("-", 1)
                return (int(ms), int(seq))
            except (ValueError, AttributeError):
                return (0, 0)

        # `max=` filter (exclusive if "(ID", else inclusive)
        if max is not None and max != "+":
            mx = max
            mx_exclusive = mx.startswith("(") if isinstance(mx, str) else False
            if mx_exclusive:
                mx = mx[1:]
            mx_key = _id_key(mx)
            if mx_exclusive:
                reversed_stream = [
                    s for s in reversed_stream if _id_key(s[0]) < mx_key
                ]
            else:
                reversed_stream = [
                    s for s in reversed_stream if _id_key(s[0]) <= mx_key
                ]
        # `min=` filter (rarely used; included for completeness)
        if min is not None and min != "-":
            mn = min
            mn_exclusive = mn.startswith("(") if isinstance(mn, str) else False
            if mn_exclusive:
                mn = mn[1:]
            mn_key = _id_key(mn)
            if mn_exclusive:
                reversed_stream = [
                    s for s in reversed_stream if _id_key(s[0]) > mn_key
                ]
            else:
                reversed_stream = [
                    s for s in reversed_stream if _id_key(s[0]) >= mn_key
                ]

        if count:
            reversed_stream = reversed_stream[:count]
        return reversed_stream

    def xlen(self, name):
        return len(self._streams.get(name, []))

    def xadd(self, name, fields, **kwargs):
        if name not in self._streams:
            self._streams[name] = []
        stream_id = f"{int(time.time() * 1000)}-{len(self._streams[name])}"
        self._streams[name].append((stream_id, fields))
        return stream_id

    # --- Key ops (for snapshot storage) ---
    def get(self, name):
        return self._keys.get(name)

    def setex(self, name, ttl, value):
        self._keys[name] = value

    # --- Connection pool (for events.py raw Redis client) ---
    @property
    def connection_pool(self):
        return self

    @property
    def connection_kwargs(self):
        return {"host": "127.0.0.1", "port": 6379}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_redis():
    return FakeRedis()


@pytest.fixture
def setup_routes(fake_redis, monkeypatch):
    """Set up the routes context module with fake Redis.

    After the Phase G symmetric refactor, every stream key is per-camera
    (events:cam1, frames:cam1, …). The dashboard reads the camera list
    from the `cameras` registry module; we stub it to a deterministic
    1-camera list so tests don't depend on a real Redis registry."""
    import routes as ctx
    ctx.r = fake_redis
    ctx.logger = __import__("logging").getLogger("test_vehicles")
    ctx.FACE_API_URL = "http://localhost:8081"
    ctx.CAMERA_ID = "cam1"
    ctx.EVENT_STREAM = "events:cam1"
    ctx.FRAME_STREAM = "frames:cam1"
    ctx.DETECTION_STREAM = "detections:pose:cam1"
    ctx.STATE_KEY = "state:cam1"
    ctx.CONFIG_KEY = "config:cam1"
    ctx.IDENTITY_KEY = "identity_state:cam1"
    ctx.ZONE_KEY = "zones:cam1"
    ctx.AUTH_DB_PATH = ""
    ctx.DEFAULT_CONFIG = {
        "confidence_thresh": "0.6",
        "iou_threshold": "0.45",
        "lost_timeout": "5",
        "target_fps": "8",
    }
    # Stub the cameras registry so routes that iterate over enabled
    # cameras (events, metrics, etc.) see exactly one test camera.
    import cameras as _cam
    monkeypatch.setattr(_cam, "enabled_camera_ids", lambda: ["cam1"])
    monkeypatch.setattr(
        _cam, "list_enabled_cameras",
        lambda: [{"id": "cam1", "name": "test", "detect_vehicles": True}],
    )
    monkeypatch.setattr(_cam, "camera_friendly_name", lambda cid: "test")
    return ctx


@pytest.fixture
def event_client(setup_routes, tmp_path):
    """FastAPI TestClient for event routes."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routes.events as events_mod
    events_mod.SNAPSHOT_DIR = str(tmp_path)
    from routes.events import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app), tmp_path


@pytest.fixture
def browse_client(setup_routes, tmp_path):
    """FastAPI TestClient for browse routes with a temp snapshot dir."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routes as ctx
    # Point VEHICLE_SNAPSHOT_DIR to temp dir
    ctx.VEHICLE_SNAPSHOT_DIR = str(tmp_path / "vehicles")
    os.makedirs(ctx.VEHICLE_SNAPSHOT_DIR, exist_ok=True)
    from routes.browse import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app), tmp_path / "vehicles"


# ===========================================================================
# Event API — Vehicle field presence
# ===========================================================================
class TestEventApiVehicleFields:
    """Verify GET /api/events returns vehicle-specific fields.

    After Phase G the route enumerates cameras via the registry and reads
    `events:<cam_id>` for each — the fixture stubs that to ['cam1'] and
    these tests seed events:cam1 directly."""

    def test_vehicle_event_has_vehicle_class(self, event_client, fake_redis, setup_routes):
        """vehicle_detected events must include vehicle_class in API response."""
        client, _ = event_client
        fake_redis._streams["events:cam1"] = [
            ("5000-0", {
                "event_type": "vehicle_detected",
                "person_id": "",
                "timestamp": "1708000000",
                "vehicle_class": "truck",
                "vehicle_confidence": "0.87",
                "snapshot_key": "vehicle_snapshot:cam1:1708000000",
                "camera_id": "cam1",
                "zone": "driveway",
                "alert_triggered": "True",
            }),
        ]
        resp = client.get("/api/events?count=10")
        events = resp.json()["events"]
        assert len(events) == 1
        evt = events[0]
        assert evt["event_type"] == "vehicle_detected"
        assert evt["vehicle_class"] == "truck"
        assert evt["vehicle_confidence"] == "0.87"
        assert evt["snapshot_key"] == "vehicle_snapshot:cam1:1708000000"

    def test_person_event_has_empty_vehicle_fields(self, event_client, fake_redis, setup_routes):
        """Person events should still work and have empty vehicle fields."""
        client, _ = event_client
        fake_redis._streams["events:cam1"] = [
            ("3000-0", {
                "event_type": "person_appeared",
                "person_id": "p42",
                "timestamp": "1708000000",
                "action": "standing",
                "camera_id": "cam1",
            }),
        ]
        resp = client.get("/api/events?count=10")
        events = resp.json()["events"]
        evt = events[0]
        assert evt["event_type"] == "person_appeared"
        assert evt["vehicle_class"] == ""
        assert evt["vehicle_confidence"] == ""
        assert evt["snapshot_key"] == ""

    def test_mixed_events_vehicle_and_person(self, event_client, fake_redis, setup_routes):
        """Both person and vehicle events in same stream handled correctly."""
        client, _ = event_client
        fake_redis._streams["events:cam1"] = [
            ("1000-0", {
                "event_type": "person_appeared",
                "person_id": "p1",
                "timestamp": "1000",
                "camera_id": "cam1",
            }),
            ("2000-0", {
                "event_type": "vehicle_detected",
                "person_id": "",
                "timestamp": "2000",
                "vehicle_class": "car",
                "vehicle_confidence": "0.95",
                "snapshot_key": "vehicle_snapshot:cam1:2000",
                "camera_id": "cam1",
            }),
        ]
        resp = client.get("/api/events?count=10")
        events = resp.json()["events"]
        assert len(events) == 2
        # xrevrange returns newest first
        vehicle_evt = events[0]
        person_evt = events[1]
        assert vehicle_evt["vehicle_class"] == "car"
        assert person_evt["vehicle_class"] == ""


# ===========================================================================
# Browse API — Day listings
# ===========================================================================
class TestBrowseDays:
    """Tests for GET /api/browse/days and day snapshot listing."""

    def test_no_snapshots_returns_empty(self, browse_client):
        """Empty snapshot directory returns empty list."""
        client, _ = browse_client
        resp = client.get("/api/browse/days")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_lists_day_folders_with_counts(self, browse_client):
        """Day folders with JPEG files are listed with correct counts."""
        client, vehicle_dir = browse_client

        # Create two day folders
        day1 = vehicle_dir / "2025-01-15"
        day2 = vehicle_dir / "2025-01-16"
        day1.mkdir(parents=True)
        day2.mkdir(parents=True)

        # Put snapshots in them
        (day1 / "10-30-00_car.jpg").write_bytes(b"\xff\xd8fake")
        (day1 / "11-00-00_truck.jpg").write_bytes(b"\xff\xd8fake")
        (day2 / "08-15-22_bus.jpg").write_bytes(b"\xff\xd8fake")

        resp = client.get("/api/browse/days")
        days = resp.json()
        assert len(days) == 2
        # Sorted newest first
        assert days[0]["date"] == "2025-01-16"
        assert days[0]["count"] == 1
        assert days[1]["date"] == "2025-01-15"
        assert days[1]["count"] == 2

    def test_non_jpg_files_not_counted(self, browse_client):
        """Only .jpg files are counted in day folders."""
        client, vehicle_dir = browse_client

        day = vehicle_dir / "2025-03-01"
        day.mkdir(parents=True)
        (day / "10-00-00_car.jpg").write_bytes(b"\xff\xd8fake")
        (day / "notes.txt").write_text("not an image")
        (day / "data.json").write_text("{}")

        resp = client.get("/api/browse/days")
        days = resp.json()
        assert days[0]["count"] == 1


class TestBrowseDaySnapshots:
    """Tests for GET /api/browse/days/{date} snapshot listing."""

    def test_list_snapshots_for_day(self, browse_client):
        """Lists snapshots for a given day with parsed time and class.

        Phase G moved snapshot storage to a per-camera subdir layout
        ({VEHICLE_SNAPSHOT_DIR}/{camera_id}/{date}/{file}). The serve URL
        now includes the camera segment: /api/browse/snapshot/<cam>/<date>/<file>.
        Snapshots written at the legacy flat path map to a '_legacy' segment."""
        client, vehicle_dir = browse_client

        day = vehicle_dir / "cam1" / "2025-02-20"
        day.mkdir(parents=True)
        (day / "14-30-55_car.jpg").write_bytes(b"\xff\xd8fake")
        (day / "15-00-10_motorcycle.jpg").write_bytes(b"\xff\xd8fake")

        resp = client.get("/api/browse/days/2025-02-20")
        assert resp.status_code == 200
        snaps = resp.json()
        assert len(snaps) == 2
        # Sorted newest first
        assert snaps[0]["time"] == "15:00:10"
        assert snaps[0]["vehicle_class"] == "motorcycle"
        assert snaps[0]["url"] == "/api/browse/snapshot/cam1/2025-02-20/15-00-10_motorcycle.jpg"
        assert snaps[0]["camera"] == "cam1"
        assert snaps[1]["time"] == "14:30:55"
        assert snaps[1]["vehicle_class"] == "car"

    def test_invalid_date_format_rejected(self, browse_client):
        """Non-YYYY-MM-DD date format returns 400."""
        client, _ = browse_client
        resp = client.get("/api/browse/days/not-a-date")
        assert resp.status_code == 400
        assert "Invalid date" in resp.json()["error"]

    def test_path_traversal_date_rejected(self, browse_client):
        """Path traversal in date parameter is blocked (returns 400 or 404)."""
        client, _ = browse_client
        resp = client.get("/api/browse/days/../../etc")
        # Either 400 (date validation) or 404 (path normalized by framework) — both safe
        assert resp.status_code in (400, 404)

    def test_nonexistent_day_returns_empty(self, browse_client):
        """Requesting a day with no folder returns empty list."""
        client, _ = browse_client
        resp = client.get("/api/browse/days/2099-12-31")
        assert resp.status_code == 200
        assert resp.json() == []


# ===========================================================================
# Browse API — Serve snapshot
# ===========================================================================
class TestBrowseServeSnapshot:
    """Tests for GET /api/browse/snapshot/{date}/{filename}."""

    def test_serve_snapshot_returns_jpeg(self, browse_client):
        """Valid snapshot path returns JPEG content."""
        client, vehicle_dir = browse_client

        day = vehicle_dir / "2025-02-20"
        day.mkdir(parents=True)
        jpeg_bytes = b"\xff\xd8\xff\xe0" + b"\x00" * 100  # Minimal JPEG-like
        (day / "10-30-00_car.jpg").write_bytes(jpeg_bytes)

        resp = client.get("/api/browse/snapshot/2025-02-20/10-30-00_car.jpg")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"
        assert resp.content == jpeg_bytes

    def test_snapshot_not_found_returns_404(self, browse_client):
        """Missing snapshot returns 404."""
        client, vehicle_dir = browse_client
        (vehicle_dir / "2025-02-20").mkdir(parents=True)

        resp = client.get("/api/browse/snapshot/2025-02-20/missing.jpg")
        assert resp.status_code == 404

    def test_snapshot_invalid_date_returns_400(self, browse_client):
        """Invalid date in snapshot URL returns 400."""
        client, _ = browse_client
        resp = client.get("/api/browse/snapshot/bad-date/file.jpg")
        assert resp.status_code == 400

    def test_snapshot_path_traversal_blocked(self, browse_client):
        """Path traversal in filename is blocked by os.path.basename."""
        client, vehicle_dir = browse_client
        day = vehicle_dir / "2025-02-20"
        day.mkdir(parents=True)

        resp = client.get("/api/browse/snapshot/2025-02-20/../../etc/passwd")
        # Should either 404 (file doesn't exist) or the path is sanitized
        assert resp.status_code in (400, 404)


# ===========================================================================
# Tracker — Vehicle tracking & idle detection
# ===========================================================================
class TestTrackerVehicleEvent:
    """Tests for tracker._process_vehicle_detections logic."""

    @pytest.fixture
    def tracker_instance(self, fake_redis):
        """Create a PersonTracker with zone support."""
        from tracker import PersonTracker
        # Extend FakeRedis for tracker needs (bytes-mode)
        fake_redis._bytes_mode = True

        class BytesFakeRedis(FakeRedis):
            """FakeRedis that mimics decode_responses=False behavior."""
            def xadd(self, name, fields, **kwargs):
                if name not in self._streams:
                    self._streams[name] = []
                sid = f"{int(time.time() * 1000)}-{len(self._streams[name])}"
                self._streams[name].append((sid, fields))
                return sid

        bfr = BytesFakeRedis()
        tracker = PersonTracker(bfr, iou_threshold=0.3, lost_timeout=5.0)
        return tracker, bfr

    def _get_events(self, r):
        """Helper to get all events from the fake Redis stream."""
        from streams import EVENT_STREAM, stream_key
        event_key = stream_key(EVENT_STREAM, camera_id="cam1")
        return r._streams.get(event_key, [])

    def test_vehicle_event_fields_complete(self, tracker_instance):
        """_process_vehicle_detections produces event with all required fields."""
        tracker, r = tracker_instance

        detections = [{
            "bbox": [100, 200, 300, 400],
            "class_name": "car",
            "confidence": 0.92,
        }]
        frame_bytes = b"\xff\xd8\xff\xe0fake_jpeg"

        tracker._process_vehicle_detections(detections, 1708000000.0, frame_bytes)

        events = self._get_events(r)
        assert len(events) == 1

        _, data = events[0]
        assert data["event_type"] == "vehicle_detected"
        assert data["vehicle_class"] == "car"
        assert data["vehicle_confidence"] == "0.92"
        assert data["snapshot_key"] != ""  # Should have a snapshot key
        assert data["camera_id"] == "cam1"

    def test_vehicle_event_per_arrival(self, tracker_instance):
        """Each newly-arriving vehicle emits its own vehicle_detected event.

        The old global rate-limit dropped legitimate events when two
        vehicles arrived within VEHICLE_RATE_LIMIT_SEC of each other.
        IoU matching already prevents same-vehicle duplicates, so we
        now emit per arrival. Two-wheelers (motorcycle/bicycle) stay
        class-strict in the fallback paths, so we use motorcycle here
        to make sure the second detection mints its own track instead
        of being absorbed via car↔truck flicker compatibility.
        """
        tracker, r = tracker_instance

        # Two different vehicles at different positions (won't IoU match)
        det1 = [{"bbox": [100, 200, 300, 400], "class_name": "motorcycle", "confidence": 0.85}]
        det2 = [{"bbox": [500, 200, 700, 400], "class_name": "car", "confidence": 0.70}]

        tracker._process_vehicle_detections(det1, 1708000000.0)
        tracker._process_vehicle_detections(det2, 1708000001.0)

        detected_events = [e for _, e in self._get_events(r) if e["event_type"] == "vehicle_detected"]
        assert len(detected_events) == 2  # Both arrivals emit

    def test_vehicle_snapshot_stored_in_redis(self, tracker_instance):
        """Vehicle snapshot bytes are stored in Redis with snapshot key.

        Key shape is `vehicle_snapshot:{camera_id}:{timestamp_ms}` —
        millisecond resolution to keep two cars arriving in the same
        second from overwriting each other's snapshot (see
        test_vehicle_snapshot_key_no_collision below).
        """
        tracker, r = tracker_instance

        jpeg = b"\xff\xd8\xff\xe0real_snapshot_data"
        detections = [{
            "bbox": [10, 20, 200, 300],
            "class_name": "bus",
            "confidence": 0.77,
        }]

        tracker._process_vehicle_detections(detections, 1708000000.0, jpeg)

        # Snapshot is stored under the ms-resolution key
        snap_key = "vehicle_snapshot:cam1:1708000000000"
        assert r._keys.get(snap_key) == jpeg

    def test_vehicle_snapshot_key_no_collision(self, tracker_instance):
        """Two vehicles arriving in the same second on the same camera
        must get distinct snapshot keys.

        Before the millisecond-resolution fix, the key was
        `vehicle_snapshot:{cam}:{int(timestamp_seconds)}` — two new
        TrackedVehicles created in the same second would overwrite each
        other's JPEG, and the second car's `vehicle_idle` Telegram would
        end up showing the first car's photo.
        """
        tracker, r = tracker_instance

        jpeg_a = b"\xff\xd8\xff\xe0car_a_snapshot"
        jpeg_b = b"\xff\xd8\xff\xe0car_b_snapshot"

        # Two distinct (non-IoU-matching) detections, 30 ms apart, same
        # whole second. Tracker creates two new TrackedVehicles, each
        # with frame_bytes → each writes its own snapshot key.
        det_a = [{"bbox": [100, 200, 300, 400], "class_name": "car", "confidence": 0.9}]
        det_b = [{"bbox": [800, 200, 1000, 400], "class_name": "truck", "confidence": 0.9}]

        tracker._process_vehicle_detections(det_a, 1708000000.000, jpeg_a)
        tracker._process_vehicle_detections(det_b, 1708000000.030, jpeg_b)

        # Two distinct snapshot keys must exist in Redis, each with the
        # right payload
        key_a = "vehicle_snapshot:cam1:1708000000000"
        key_b = "vehicle_snapshot:cam1:1708000000030"
        assert r._keys.get(key_a) == jpeg_a
        assert r._keys.get(key_b) == jpeg_b
        # Sanity: snapshots are not the same key
        assert key_a != key_b

    def test_vehicle_event_no_snapshot_without_frame_bytes(self, tracker_instance):
        """Without frame_bytes, snapshot_key should be empty."""
        tracker, r = tracker_instance

        detections = [{
            "bbox": [10, 20, 200, 300],
            "class_name": "motorcycle",
            "confidence": 0.65,
        }]

        tracker._process_vehicle_detections(detections, 1708000000.0, None)

        events = self._get_events(r)
        assert len(events) == 1
        _, data = events[0]
        assert data["snapshot_key"] == ""

    # -----------------------------------------------------------------------
    # Helper: feed stationary detections to build center_history ≥ 5
    # -----------------------------------------------------------------------
    def _feed_stationary(self, tracker, det, t_start, n_frames=6, jpeg=None):
        """Feed the same bbox n_frames times, 1s apart, to fill center_history."""
        for i in range(n_frames):
            tracker._process_vehicle_detections(det, t_start + i, jpeg)

    # -----------------------------------------------------------------------
    # Vehicle idle detection tests (updated for 90s timeout + stationarity)
    # -----------------------------------------------------------------------
    def test_vehicle_idle_fires_after_timeout(self, tracker_instance):
        """vehicle_idle fires when stationary for ≥ VEHICLE_IDLE_TIMEOUT (90s)."""
        tracker, r = tracker_instance

        det = [{"bbox": [100, 200, 300, 400], "class_name": "car", "confidence": 0.9}]
        jpeg = b"\xff\xd8\xff\xe0snapshot"

        # Feed 6 frames at same position to satisfy is_stationary (needs ≥5)
        self._feed_stationary(tracker, det, 1708000000.0, n_frames=6, jpeg=jpeg)

        # Jump to t=91s — past 90s idle timeout → vehicle_idle
        tracker._process_vehicle_detections(det, 1708000091.0, jpeg)

        events = self._get_events(r)
        event_types = [e["event_type"] for _, e in events]

        assert "vehicle_detected" in event_types
        assert "vehicle_idle" in event_types

        # Verify idle event has correct duration
        idle_events = [(_, e) for _, e in events if e["event_type"] == "vehicle_idle"]
        assert len(idle_events) == 1
        _, idle_data = idle_events[0]
        assert float(idle_data["duration"]) >= 90.0
        assert idle_data["vehicle_class"] == "car"

    def test_vehicle_idle_not_before_timeout(self, tracker_instance):
        """vehicle_idle does NOT fire before VEHICLE_IDLE_TIMEOUT (90s)."""
        tracker, r = tracker_instance

        det = [{"bbox": [100, 200, 300, 400], "class_name": "truck", "confidence": 0.8}]

        # Feed frames up to t=60s — not past 90s threshold
        self._feed_stationary(tracker, det, 1708000000.0, n_frames=6)
        tracker._process_vehicle_detections(det, 1708000060.0)

        events = self._get_events(r)
        event_types = [e["event_type"] for _, e in events]
        assert "vehicle_idle" not in event_types

    def test_two_vehicles_tracked_independently(self, tracker_instance):
        """Two vehicles at different positions are tracked as separate objects."""
        tracker, r = tracker_instance

        # Vehicle A (left side) and Vehicle B (right side) — no IoU overlap
        # Use motorcycle as the second class so the fallback car↔truck
        # flicker compatibility doesn't merge two physically distinct
        # vehicles in the same frame.
        det_both = [
            {"bbox": [10, 100, 150, 250], "class_name": "car", "confidence": 0.9},
            {"bbox": [400, 100, 550, 250], "class_name": "motorcycle", "confidence": 0.85},
        ]

        # Feed 6 frames to build center_history for both
        self._feed_stationary(tracker, det_both, 1708000000.0, n_frames=6)
        assert len(tracker.tracked_vehicles) == 2

        # t=91s — both still there, both should idle
        tracker._process_vehicle_detections(det_both, 1708000091.0)

        events = self._get_events(r)
        idle_events = [e for _, e in events if e["event_type"] == "vehicle_idle"]
        assert len(idle_events) == 2  # Both vehicles idled

    def test_stale_vehicles_pruned(self, tracker_instance):
        """Vehicles not seen for > VEHICLE_LOST_TIMEOUT are removed."""
        tracker, r = tracker_instance

        det = [{"bbox": [100, 200, 300, 400], "class_name": "car", "confidence": 0.9}]

        # Vehicle appears at t=0
        tracker._process_vehicle_detections(det, 1708000000.0)
        assert len(tracker.tracked_vehicles) == 1

        # Different vehicle at t=15 (original is stale after 10s). Use
        # motorcycle so the car↔bus flicker compatibility doesn't let
        # it inherit the original car track via ghost-match.
        det2 = [{"bbox": [500, 200, 700, 400], "class_name": "motorcycle", "confidence": 0.7}]
        tracker._process_vehicle_detections(det2, 1708000015.0)

        # Original vehicle should be pruned, only new one remains
        assert len(tracker.tracked_vehicles) == 1
        remaining = list(tracker.tracked_vehicles.values())[0]
        assert remaining.class_name == "motorcycle"


# ===========================================================================
# TrackedVehicle — is_stationary, center_history, snapshot_bbox
# ===========================================================================
class TestTrackedVehicleStationary:
    """Unit tests for TrackedVehicle.is_stationary and related state.

    is_stationary compares the CURRENT center against the MEDIAN of the
    rolling 20-sample history (~4s at 5 FPS). Threshold scales with bbox
    width: max(8 px, bbox_w * 0.10). The default test bbox is 200 px wide
    → threshold = 20 px. Requires ≥ 5 samples; fewer always returns False."""

    def _make_vehicle(self, bbox=None, timestamp=0.0):
        """Create a TrackedVehicle with default values."""
        from tracker import TrackedVehicle
        bbox = bbox or [100, 200, 300, 400]
        return TrackedVehicle(
            vehicle_id="v0001",
            bbox=bbox,
            class_name="car",
            confidence=0.9,
            timestamp=timestamp,
        )

    def test_stationary_when_not_moving(self):
        """Vehicle at the same position for 10 frames → is_stationary == True."""
        veh = self._make_vehicle()
        # Feed same bbox 9 more times (1 from __init__ + 9 = 10 total)
        for i in range(1, 10):
            veh.update([100, 200, 300, 400], "car", 0.9, float(i))
        assert len(veh.center_history) == 10
        assert veh.is_stationary is True

    def test_not_stationary_when_moving(self):
        """Vehicle shifting 50px per frame → is_stationary == False."""
        veh = self._make_vehicle()
        for i in range(1, 10):
            shifted_bbox = [100 + i * 50, 200, 300 + i * 50, 400]
            veh.update(shifted_bbox, "car", 0.9, float(i))
        assert veh.is_stationary is False

    def test_not_stationary_with_few_frames(self):
        """< 5 frames → always False (not enough data)."""
        veh = self._make_vehicle()
        veh.update([100, 200, 300, 400], "car", 0.9, 1.0)
        veh.update([100, 200, 300, 400], "car", 0.9, 2.0)
        assert len(veh.center_history) == 3
        assert veh.is_stationary is False

    def test_stationary_at_exactly_5_frames(self):
        """Exactly 5 frames at same position → is_stationary == True."""
        veh = self._make_vehicle()
        for i in range(1, 5):  # 1 from init + 4 = 5 total
            veh.update([100, 200, 300, 400], "car", 0.9, float(i))
        assert len(veh.center_history) == 5
        assert veh.is_stationary is True

    def test_center_history_capped_at_20(self):
        """After 25 updates, center_history should be capped at 20."""
        veh = self._make_vehicle()
        for i in range(1, 26):  # 1 from init + 25 = 26 attempts
            veh.update([100, 200, 300, 400], "car", 0.9, float(i))
        assert len(veh.center_history) == 20

    def test_stationary_boundary_under_threshold(self):
        """bbox_w=200, threshold = max(20, 200*0.15) = 30 px. Drift of 19 px
        is well under that → still stationary."""
        veh = self._make_vehicle(bbox=[100, 200, 300, 400])
        # 5 same-position updates so the median sits at (200, 300)
        for i in range(1, 6):
            veh.update([100, 200, 300, 400], "car", 0.9, float(i))
        # Shift current bbox so its center is 19 px to the right of median
        veh.update([119, 200, 319, 400], "car", 0.9, 6.0)
        assert veh.is_stationary is True

    def test_stationary_boundary_over_threshold(self):
        """bbox_w=200, threshold = 30 px. Drift of 31 px exceeds it →
        not stationary. (Threshold was bumped from 20 → 30 to absorb
        YOLO bbox jitter on parked cars; see state.py comment + the
        cam1 live-data analysis that motivated the change.)"""
        veh = self._make_vehicle(bbox=[100, 200, 300, 400])
        for i in range(1, 6):
            veh.update([100, 200, 300, 400], "car", 0.9, float(i))
        # Final frame: shift current bbox center by 31 px (just over)
        veh.update([131, 200, 331, 400], "car", 0.9, 6.0)
        assert veh.is_stationary is False

    def test_stationary_resists_small_jitter(self):
        """Real YOLO jitter on a parked car (5-8 px frame-to-frame) must
        NOT flip is_stationary False. Cam1 live data showed the old 8 px
        threshold doing this and causing `idle_alerted` to reset → same
        TrackedVehicle re-emitting `vehicle_idle` events every few minutes.
        """
        veh = self._make_vehicle(bbox=[100, 200, 300, 400])
        # Realistic jitter sequence — small horizontal nudges
        for i, dx in enumerate([0, 2, -3, 4, -2, 5, -4, 3, -6, 7], start=1):
            veh.update([100 + dx, 200, 300 + dx, 400], "car", 0.9, float(i))
        # Final small-drift sample
        veh.update([108, 202, 308, 402], "car", 0.9, 11.0)
        # bbox_w=200 → threshold 30 px. Drift is well under → stationary.
        assert veh.is_stationary is True

    def test_snapshot_bbox_preserved_after_update(self):
        """snapshot_bbox should stay at initial bbox even after updates."""
        veh = self._make_vehicle(bbox=[100, 200, 300, 400])
        assert veh.snapshot_bbox == [100, 200, 300, 400]
        veh.update([110, 210, 310, 410], "car", 0.9, 1.0)
        # snapshot_bbox should not change from update()
        assert veh.snapshot_bbox == [100, 200, 300, 400]
        # bbox should be the latest
        assert veh.bbox == [110, 210, 310, 410]

    def test_idle_requires_stationary(self, fake_redis):
        """Vehicle tracked > 90s but moving → no vehicle_idle event."""
        from tracker import PersonTracker

        class BytesFakeRedis(FakeRedis):
            def xadd(self, name, fields, **kwargs):
                if name not in self._streams:
                    self._streams[name] = []
                sid = f"{int(time.time() * 1000)}-{len(self._streams[name])}"
                self._streams[name].append((sid, fields))
                return sid

        bfr = BytesFakeRedis()
        tracker = PersonTracker(bfr, iou_threshold=0.3, lost_timeout=5.0)

        # Moving vehicle: shift 50px every frame so is_stationary == False
        for i in range(7):
            det = [{"bbox": [100 + i * 50, 200, 300 + i * 50, 400],
                    "class_name": "car", "confidence": 0.9}]
            tracker._process_vehicle_detections(det, 1708000000.0 + i * 2)

        # Jump to t=91s — past timeout but vehicle was moving
        det_final = [{"bbox": [100 + 7 * 50, 200, 300 + 7 * 50, 400],
                      "class_name": "car", "confidence": 0.9}]
        tracker._process_vehicle_detections(det_final, 1708000091.0)

        from streams import EVENT_STREAM, stream_key
        event_key = stream_key(EVENT_STREAM, camera_id="cam1")
        events = bfr._streams.get(event_key, [])
        event_types = [e["event_type"] for _, e in events]
        assert "vehicle_idle" not in event_types  # Moving → no idle


# ===========================================================================
# Ghost-buffer re-association (PersonTracker._try_ghost_match)
# ===========================================================================
class TestVehicleGhostBuffer:
    """When a vehicle goes stale (vehicle_lost_timeout) it's moved into
    `_ghost_vehicles`. A new same-class detection within
    VEHICLE_GHOST_MAX_DIST_RATIO × bbox_width of the ghost's last center
    re-associates instead of creating a new vehicle_id. Eliminates the
    detected → left → detected triple-event for one car driving through
    a dead-zone."""

    def _new_tracker(self, fake_redis):
        from tracker import PersonTracker
        return PersonTracker(fake_redis, iou_threshold=0.3, lost_timeout=5.0)

    def _seed_ghost(self, tracker, bbox=None, class_name="car",
                    vehicle_id="v0001", ghost_ts=1.0):
        """Put a TrackedVehicle directly into the ghost buffer."""
        from tracker import TrackedVehicle
        bbox = bbox or [100, 200, 300, 400]
        veh = TrackedVehicle(
            vehicle_id=vehicle_id,
            bbox=bbox,
            class_name=class_name,
            confidence=0.9,
            timestamp=ghost_ts,
        )
        tracker._ghost_vehicles[vehicle_id] = (veh, ghost_ts)
        return veh

    def test_empty_buffer_returns_none(self, fake_redis):
        """No ghosts → no match, regardless of input."""
        tracker = self._new_tracker(fake_redis)
        assert tracker._try_ghost_match([100, 200, 300, 400], "car", 5.0) is None

    def test_close_same_class_matches(self, fake_redis):
        """Same class, center within max_dist (2 × bbox_width by default)
        → returns the ghost's id."""
        tracker = self._new_tracker(fake_redis)
        self._seed_ghost(tracker, bbox=[100, 200, 300, 400])  # bbox_w = 200
        # New detection 50px to the right (well within 2 * 200 = 400 px)
        result = tracker._try_ghost_match([150, 200, 350, 400], "car", 5.0)
        assert result == "v0001"

    def test_different_class_does_not_match(self, fake_redis):
        """Strict class mismatch: a car ghost must NOT re-associate with
        a motorcycle. The car↔truck flicker compatibility introduced in
        a later fix does allow car↔truck/bus to ghost-match, but two-
        wheelers (motorcycle/bicycle) stay strict because YOLO almost
        never confuses those with 4-wheel vehicles."""
        tracker = self._new_tracker(fake_redis)
        self._seed_ghost(tracker, bbox=[100, 200, 300, 400], class_name="car")
        # Motorcycle detection at the same position
        result = tracker._try_ghost_match([100, 200, 300, 400], "motorcycle", 5.0)
        assert result is None

    def test_too_far_does_not_match(self, fake_redis):
        """Center beyond bbox_width × VEHICLE_GHOST_MAX_DIST_RATIO → no match.
        bbox_w = 200, ratio default 3.5 → max_dist = 700 px. We test with an
        800-px shift to stay clearly outside even the wider post-bump threshold."""
        tracker = self._new_tracker(fake_redis)
        self._seed_ghost(tracker, bbox=[100, 200, 300, 400])  # center (200,300)
        # Detection 800px to the right — center (1000, 300) → dist 800 > 700
        result = tracker._try_ghost_match([900, 200, 1100, 400], "car", 5.0)
        assert result is None

    def test_picks_nearest_when_multiple_candidates(self, fake_redis):
        """With two same-class ghosts in range, pick the closer one."""
        tracker = self._new_tracker(fake_redis)
        # Ghost A center (200, 300)
        self._seed_ghost(tracker, bbox=[100, 200, 300, 400], vehicle_id="vA")
        # Ghost B center (500, 300) — 100px to the right of new detection
        self._seed_ghost(tracker, bbox=[400, 200, 600, 400], vehicle_id="vB")
        # New detection center (410, 300) — closer to vB than vA
        result = tracker._try_ghost_match([310, 200, 510, 400], "car", 5.0)
        assert result == "vB"

    def test_re_association_keeps_first_seen(self, fake_redis):
        """When ghost is rehydrated via _process_vehicle_detections, the
        vehicle's first_seen should NOT reset — the rolling duration is
        the value of the ghost buffer."""
        tracker = self._new_tracker(fake_redis)
        # Push a vehicle through the normal path so the snapshot path is happy
        det = [{"bbox": [100, 200, 300, 400], "class_name": "car",
                "confidence": 0.9}]
        tracker._process_vehicle_detections(det, 1000.0)
        first_seen = list(tracker.tracked_vehicles.values())[0].first_seen
        # Simulate vehicle going stale — drop it directly into ghost buffer
        vid, veh = next(iter(tracker.tracked_vehicles.items()))
        tracker._ghost_vehicles[vid] = (veh, 1010.0)
        del tracker.tracked_vehicles[vid]
        # Re-detect 1s later (within VEHICLE_GHOST_TTL=5s default)
        det2 = [{"bbox": [120, 200, 320, 400], "class_name": "car",
                 "confidence": 0.9}]
        tracker._process_vehicle_detections(det2, 1011.0)
        # The re-associated vehicle should keep the original first_seen
        rehydrated = list(tracker.tracked_vehicles.values())[0]
        assert rehydrated.first_seen == first_seen, (
            f"Ghost re-association reset first_seen — duration counter "
            f"will misreport ({rehydrated.first_seen} vs {first_seen})"
        )


# ===========================================================================
# Tracker — vehicle_sample event emission (Phase 1 vehicle-attributes)
# ===========================================================================
def test_tracker_emits_vehicle_sample_every_n_updates_after_eager_window(monkeypatch):
    """With EAGER_SAMPLE_FRAMES=0 (eager window disabled) and
    SAMPLE_INTERVAL_FRAMES=3: the spawn frame still emits a sample (the
    new-vehicle branch is unconditional), then the every-Nth gate fires at
    fc=3, 6, ... Earlier behavior — with no spawn sample — meant a 4-frame
    visit produced 1 sample; under the spawn-sample fix the same visit
    produces 2 (spawn + fc=3)."""
    monkeypatch.setenv("EMIT_VEHICLE_SAMPLES", "1")
    monkeypatch.setenv("SAMPLE_INTERVAL_FRAMES", "3")
    monkeypatch.setenv("EAGER_SAMPLE_FRAMES", "0")
    monkeypatch.setenv("CAMERA_ID", "cam1")
    import importlib
    from services.tracker.core import manager as mgr
    importlib.reload(mgr)

    fake = FakeRedis()
    m = mgr.Manager(fake)

    bbox = [100, 100, 200, 200]
    for t in range(4):
        m._process_vehicle_detections([{"bbox": bbox, "class_name": "car",
                                        "confidence": 0.8}], timestamp=float(t))

    events = [fields for _id, fields in fake._streams.get("events:cam1", [])]
    sample_events = [e for e in events if e.get("event_type") == "vehicle_sample"]
    detected_events = [e for e in events if e.get("event_type") == "vehicle_detected"]

    assert len(detected_events) == 1
    # spawn (fc=1) + fc=3 matches → 2 samples
    assert len(sample_events) == 2
    assert all(json.loads(s["bbox"]) == bbox for s in sample_events)


def test_tracker_short_drive_by_three_detections_produces_three_samples(monkeypatch):
    """User-facing guarantee: with the default eager window (8 frames) any
    short drive-by where the detector hits the same vehicle N≤8 times
    produces N samples, not 1 or 2. Before the spawn-sample + eager-window
    fix, 3 detector hits produced 1 sample (the new-vehicle branch never
    emitted, and the every-3rd gate only fired once at fc=3). Now: 3."""
    monkeypatch.setenv("EMIT_VEHICLE_SAMPLES", "1")
    monkeypatch.setenv("SAMPLE_INTERVAL_FRAMES", "3")
    monkeypatch.delenv("EAGER_SAMPLE_FRAMES", raising=False)  # default = 8
    monkeypatch.setenv("CAMERA_ID", "cam1")
    import importlib
    from services.tracker.core import manager as mgr
    importlib.reload(mgr)

    fake = FakeRedis()
    m = mgr.Manager(fake)

    bbox = [100, 100, 200, 200]
    for t in range(3):
        m._process_vehicle_detections([{"bbox": bbox, "class_name": "car",
                                        "confidence": 0.8}], timestamp=float(t))

    events = [fields for _id, fields in fake._streams.get("events:cam1", [])]
    sample_events = [e for e in events if e.get("event_type") == "vehicle_sample"]
    assert len(sample_events) == 3


def test_tracker_single_frame_visit_produces_one_sample(monkeypatch):
    """A vehicle the detector sees only once — single-frame appearance —
    must still produce one buffered crop. Before the spawn-sample fix this
    produced zero samples and the per-track dir was never written
    (empty-buffer skip in storage.py:43)."""
    monkeypatch.setenv("EMIT_VEHICLE_SAMPLES", "1")
    monkeypatch.setenv("CAMERA_ID", "cam1")
    import importlib
    from services.tracker.core import manager as mgr
    importlib.reload(mgr)

    fake = FakeRedis()
    m = mgr.Manager(fake)

    m._process_vehicle_detections([{"bbox": [100, 100, 200, 200], "class_name": "car",
                                    "confidence": 0.8}], timestamp=0.0)

    events = [fields for _id, fields in fake._streams.get("events:cam1", [])]
    sample_events = [e for e in events if e.get("event_type") == "vehicle_sample"]
    assert len(sample_events) == 1


def test_tracker_long_track_falls_back_to_throttle_after_eager_window(monkeypatch):
    """For a long parked-car track, sample emission must throttle to
    SAMPLE_INTERVAL_FRAMES after the EAGER_SAMPLE_FRAMES window — otherwise
    we 3x the Redis HD-sample write cost on every persistent track."""
    monkeypatch.setenv("EMIT_VEHICLE_SAMPLES", "1")
    monkeypatch.setenv("SAMPLE_INTERVAL_FRAMES", "3")
    monkeypatch.setenv("EAGER_SAMPLE_FRAMES", "8")
    monkeypatch.setenv("CAMERA_ID", "cam1")
    import importlib
    from services.tracker.core import manager as mgr
    importlib.reload(mgr)

    fake = FakeRedis()
    m = mgr.Manager(fake)

    bbox = [100, 100, 200, 200]
    # 20 detections: fc=1 spawn + fc=2..20 matches
    for t in range(20):
        m._process_vehicle_detections([{"bbox": bbox, "class_name": "car",
                                        "confidence": 0.8}], timestamp=float(t))

    events = [fields for _id, fields in fake._streams.get("events:cam1", [])]
    sample_events = [e for e in events if e.get("event_type") == "vehicle_sample"]
    # Eager: fc=1..8 (8 samples). Throttled: fc=9, 12, 15, 18 (4 samples).
    # fc=21 would be next but we stop at fc=20. Total = 12.
    assert len(sample_events) == 12


def test_tracker_does_not_emit_sample_when_feature_disabled(monkeypatch):
    """Default env (EMIT_VEHICLE_SAMPLES unset) ⇒ zero sample events even
    across many matched updates."""
    monkeypatch.delenv("EMIT_VEHICLE_SAMPLES", raising=False)
    monkeypatch.setenv("CAMERA_ID", "cam1")
    import importlib
    from services.tracker.core import manager as mgr
    importlib.reload(mgr)

    fake = FakeRedis()
    m = mgr.Manager(fake)
    bbox = [100, 100, 200, 200]
    for t in range(0, 10):
        m._process_vehicle_detections([{"bbox": bbox, "class_name": "car",
                                        "confidence": 0.8}], timestamp=float(t))

    events = [fields for _id, fields in fake._streams.get("events:cam1", [])]
    samples = [e for e in events if e.get("event_type") == "vehicle_sample"]
    assert samples == []


# ===========================================================================
# IoU identity-swap regression — same car across consecutive frames whose
# bboxes shifted enough to drop below the IoU threshold should still match
# the existing TrackedVehicle, not spawn a new one. Live cam1 data showed
# this failing with bboxes [756.4, 320.7, 830.3, 355.8] (frame N) →
# [706.7, 329.3, 781.2, 366.6] (frame N+1, 225ms later, IoU≈0.14). Two
# TrackedVehicles were created for the same physical car, producing two
# vehicle_detected events + later two vehicle_gone events.
# ===========================================================================
def test_drive_by_with_low_iou_consecutive_frames_does_not_double_track(monkeypatch):
    """Two consecutive detections of the SAME physical car whose bboxes
    shifted enough to have IoU < threshold (fast-moving / large between-
    frame motion). Must match to the same TrackedVehicle via the
    center-distance fallback, not spawn a second one."""
    monkeypatch.setenv("CAMERA_ID", "cam1")
    import importlib
    from services.tracker.core import manager as mgr
    importlib.reload(mgr)

    fake = FakeRedis()
    m = mgr.Manager(fake)

    # Live data from cam1, 2026-05-21 — same car 225ms apart, IoU≈0.14
    m._process_vehicle_detections(
        [{"bbox": [756.4, 320.7, 830.3, 355.8],
          "class_name": "car", "confidence": 0.82}],
        timestamp=0.0,
    )
    m._process_vehicle_detections(
        [{"bbox": [706.7, 329.3, 781.2, 366.6],
          "class_name": "car", "confidence": 0.78}],
        timestamp=0.225,
    )

    # Exactly one TrackedVehicle should exist
    assert len(m.tracked_vehicles) == 1, (
        f"expected 1 tracked vehicle (IoU swap should match via center "
        f"fallback), got {len(m.tracked_vehicles)}: "
        f"{list(m.tracked_vehicles.keys())}"
    )

    # Exactly one vehicle_detected event in the stream
    events = [fields for _id, fields in fake._streams.get("events:cam1", [])]
    detected = [e for e in events if e.get("event_type") == "vehicle_detected"]
    assert len(detected) == 1, (
        f"expected 1 vehicle_detected, got {len(detected)}: {events}"
    )


def test_drive_by_with_225px_shift_in_one_second_does_not_double_track(monkeypatch):
    """Reproduces the exact cam1 case observed 2026-05-21: a fast-moving car
    on a wide-angle fish-eye cam jumped from bbox [461, 372, 559, 409] to
    [688, 331, 776, 374] in 1.1 s — center shift of 225 px, IoU=0. The
    original bbox_w*2.0 threshold (~196 px) missed this; bbox_w*3.5 (~343 px)
    catches it. Must remain a single track."""
    monkeypatch.setenv("CAMERA_ID", "cam1")
    import importlib
    from services.tracker.core import manager as mgr
    importlib.reload(mgr)

    fake = FakeRedis()
    m = mgr.Manager(fake)
    m._process_vehicle_detections(
        [{"bbox": [461, 372, 559, 409],
          "class_name": "car", "confidence": 0.8}],
        timestamp=0.0,
    )
    m._process_vehicle_detections(
        [{"bbox": [688, 331, 776, 374],
          "class_name": "car", "confidence": 0.8}],
        timestamp=1.1,
    )
    assert len(m.tracked_vehicles) == 1, (
        f"same physical car split into {len(m.tracked_vehicles)} TrackedVehicles; "
        f"VEHICLE_GHOST_MAX_DIST_RATIO too tight for fast-moving cars"
    )


def test_two_genuinely_different_cars_far_apart_dont_merge(monkeypatch):
    """Two cars at very different positions in the frame should NOT match
    via center-distance fallback. Verifies the fallback isn't over-eager
    after the 2.0 → 3.5 bump. Center distance ~750 px is still well beyond
    `bbox_w * 3.5 = 280 px` for 80-px-wide cars."""
    monkeypatch.setenv("CAMERA_ID", "cam1")
    import importlib
    from services.tracker.core import manager as mgr
    importlib.reload(mgr)

    fake = FakeRedis()
    m = mgr.Manager(fake)

    # Car 1 at left edge, Car 2 at right edge — same camera frame
    m._process_vehicle_detections(
        [{"bbox": [50, 320, 130, 360],   # left-side car
          "class_name": "car", "confidence": 0.8}],
        timestamp=0.0,
    )
    m._process_vehicle_detections(
        [{"bbox": [800, 320, 880, 360],  # right-side car, ~750px away
          "class_name": "car", "confidence": 0.8}],
        timestamp=0.225,
    )

    assert len(m.tracked_vehicles) == 2, (
        f"expected 2 distinct tracks for far-apart cars, got "
        f"{len(m.tracked_vehicles)}"
    )


def test_center_fallback_respects_class_match(monkeypatch):
    """A car and a motorcycle with low IoU but close centers must NOT
    merge via the center-distance fallback. The car↔truck/bus flicker
    relaxation in _class_compatible doesn't extend to two-wheelers —
    they're visually distinct enough that YOLO almost never confuses
    them. The existing IoU step is class-blind for primary matching,
    which handles the within-family flicker case."""
    monkeypatch.setenv("CAMERA_ID", "cam1")
    import importlib
    from services.tracker.core import manager as mgr
    importlib.reload(mgr)

    fake = FakeRedis()
    m = mgr.Manager(fake)

    # Two adjacent bboxes, no overlap → IoU=0 (below threshold). Centers
    # are 80px apart, bbox_w=60, so threshold = 60×2.0 = 120 → within range.
    # If class check is skipped on the fallback, these would incorrectly
    # merge. With the check, they stay separate.
    m._process_vehicle_detections(
        [{"bbox": [400, 300, 460, 340],
          "class_name": "car", "confidence": 0.8}],
        timestamp=0.0,
    )
    m._process_vehicle_detections(
        [{"bbox": [480, 300, 540, 340],
          "class_name": "motorcycle", "confidence": 0.8}],
        timestamp=0.1,
    )
    assert len(m.tracked_vehicles) == 2


# ===========================================================================
# vehicle_gone + vehicle_left semantic split (Phase 1 follow-up fix)
# ===========================================================================
def test_drive_by_emits_vehicle_gone_but_not_vehicle_left(monkeypatch):
    """A drive-by car (never went idle) should emit vehicle_gone at ghost
    expiry but NOT vehicle_left. The vehicle_left event is reserved for
    user-facing idle-leave notifications — see contracts/streams.py."""
    monkeypatch.setenv("CAMERA_ID", "cam1")
    import importlib
    from services.tracker.core import manager as mgr
    importlib.reload(mgr)
    from services.tracker.core.config import (
        VEHICLE_LOST_TIMEOUT, VEHICLE_GHOST_TTL,
    )

    fake = FakeRedis()
    m = mgr.Manager(fake)
    bbox = [100, 100, 200, 200]

    # Drive-by: appear at t=0, never go idle (default vehicle_idle_timeout
    # is 90s; we'll only run for a few seconds).
    m._process_vehicle_detections(
        [{"bbox": bbox, "class_name": "car", "confidence": 0.8}],
        timestamp=0.0,
    )
    # Advance past LOST_TIMEOUT + GHOST_TTL with no new detection so the
    # ghost expires and the relevant emit fires.
    # First sweep at LOST_TIMEOUT+ moves the vehicle into the ghost buffer
    # (ghost_ts = this timestamp). Second sweep at GHOST_TTL+ past that
    # ghost_ts is what actually fires the ghost-expiry emit.
    stale_t = VEHICLE_LOST_TIMEOUT + 1.0
    m._process_vehicle_detections([], timestamp=stale_t)
    expire_t = stale_t + VEHICLE_GHOST_TTL + 1.0
    m._process_vehicle_detections([], timestamp=expire_t)

    events = [fields for _id, fields in fake._streams.get("events:cam1", [])]
    gone = [e for e in events if e.get("event_type") == "vehicle_gone"]
    left = [e for e in events if e.get("event_type") == "vehicle_left"]

    assert len(gone) == 1, f"expected 1 vehicle_gone, got {len(gone)}: {events}"
    assert gone[0]["was_idle"] == "False"
    assert left == [], f"expected NO vehicle_left for drive-by, got {left}"


def test_stationary_but_not_yet_idle_alerted_track_rejects_drive_by_merge(monkeypatch):
    """Contract: a car that's stopped moving but hasn't been idle for the
    full vehicle_idle_timeout (150 s default) must STILL be protected
    from drive-by merges. Observed live on cam1 — vehicle_0011 (parked
    Honda) absorbed two passing-car bboxes at +1 s and +40 s after
    detection, well before idle_alerted would have fired at +150 s. The
    tight-IoU gate must trigger on `is_stationary or idle_alerted`, not
    idle_alerted alone."""
    monkeypatch.setenv("CAMERA_ID", "cam1")
    import importlib
    from services.tracker.core import manager as mgr
    importlib.reload(mgr)

    fake = FakeRedis()
    m = mgr.Manager(fake)

    # Park a car and feed 6 frames at the same bbox so is_stationary
    # flips True (needs ≥5 center-history samples). We do NOT mark
    # idle_alerted=True — vehicle_idle_timeout hasn't elapsed yet.
    for i in range(6):
        m._process_vehicle_detections(
            [{"bbox": [100, 100, 200, 200], "class_name": "car",
              "confidence": 0.85}],
            timestamp=float(i) * 0.2,
        )
    parked = next(iter(m.tracked_vehicles.values()))
    assert parked.is_stationary, "test setup: track should be stationary"
    assert not parked.idle_alerted, \
        "test setup: idle_alerted shouldn't fire this early"

    # A drive-by car passes with bbox [120, 120, 220, 220] — overlaps the
    # parked car's bbox at IoU ~0.47, well above the regular 0.2 threshold
    # but well below the 0.65 idle-IoU threshold. Must NOT merge: this is
    # the bug class that polluted vehicle_0011's track buffer in prod.
    m._process_vehicle_detections(
        [{"bbox": [120, 120, 220, 220], "class_name": "car",
          "confidence": 0.78}],
        timestamp=2.0,
    )

    assert len(m.tracked_vehicles) == 2, (
        f"drive-by must NOT merge into stationary (but not-yet-idle) "
        f"track; tracks="
        f"{[(v.vehicle_id, v.bbox, v.idle_alerted, v.is_stationary) for v in m.tracked_vehicles.values()]}"
    )


def test_idle_track_rejects_low_iou_drive_by_match(monkeypatch):
    """Contract: a parked (idle-confirmed) car must NOT absorb a drive-by
    car that briefly overlaps its bbox at modest IoU. Observed live on
    cam1: vehicle_0001 was a parked car that captured 8 crops over 152 s.
    One of those crops (angle_05) was a different physical vehicle — an
    SUV that drove through the parked car's bbox region with IoU ~0.3.
    The classifier vote then split between the two vehicles' colors /
    body types, dragging confidence down under the 0.55 threshold.

    Fix: when a tracked vehicle is idle_alerted, require
    VEHICLE_IDLE_IOU_THRESHOLD (0.65 default) before merging — only
    near-perfect bbox overlap is accepted. Drive-bys fail to merge and
    correctly mint their own track."""
    monkeypatch.setenv("CAMERA_ID", "cam1")
    import importlib
    from services.tracker.core import manager as mgr
    importlib.reload(mgr)

    fake = FakeRedis()
    m = mgr.Manager(fake)

    # Parked car at bbox [100, 100, 200, 200] — mark idle directly.
    m._process_vehicle_detections(
        [{"bbox": [100, 100, 200, 200], "class_name": "car",
          "confidence": 0.85}],
        timestamp=0.0,
    )
    parked = next(iter(m.tracked_vehicles.values()))
    parked.idle_alerted = True
    assert len(m.tracked_vehicles) == 1

    # A drive-by SUV passes with bbox [120, 120, 220, 220] — overlaps the
    # parked car's bbox at IoU ~0.39, well above the regular 0.2 threshold
    # but well below the 0.65 idle threshold. Must NOT merge.
    m._process_vehicle_detections(
        [{"bbox": [120, 120, 220, 220], "class_name": "car",
          "confidence": 0.78}],
        timestamp=2.0,
    )

    # Two distinct tracks now: the parked car AND the drive-by.
    assert len(m.tracked_vehicles) == 2, (
        f"drive-by must NOT merge into idle track; tracks="
        f"{[(v.vehicle_id, v.bbox, v.idle_alerted) for v in m.tracked_vehicles.values()]}"
    )


def test_idle_track_accepts_high_iou_re_detection_of_same_car(monkeypatch):
    """Contract: the IoU tightening must NOT block the parked car itself.
    When the detector re-finds the SAME parked car at nearly the same bbox
    (IoU >=0.65 — typical for a stationary YOLO bbox with sub-pixel jitter),
    the existing track must continue, NOT a new one minted."""
    monkeypatch.setenv("CAMERA_ID", "cam1")
    import importlib
    from services.tracker.core import manager as mgr
    importlib.reload(mgr)

    fake = FakeRedis()
    m = mgr.Manager(fake)
    m._process_vehicle_detections(
        [{"bbox": [100, 100, 200, 200], "class_name": "car",
          "confidence": 0.85}],
        timestamp=0.0,
    )
    parked = next(iter(m.tracked_vehicles.values()))
    parked.idle_alerted = True

    # Same car re-detected with tiny bbox jitter — IoU ~0.94, well above
    # the 0.65 idle threshold.
    m._process_vehicle_detections(
        [{"bbox": [103, 103, 203, 203], "class_name": "car",
          "confidence": 0.85}],
        timestamp=2.0,
    )
    assert len(m.tracked_vehicles) == 1, "same parked car must keep its track"


def test_idle_track_absorbs_jittered_wider_bbox_via_iom_escape_hatch(monkeypatch):
    """Live regression — duplicate vehicle_idle at 12:25 on cam1.

    The parked Honda was tracked at bbox [734.8, 323.8, 809.6, 358.4]
    (w=74.8 h=34.6). YOLO then emitted a detection at
    [735.4, 317.4, 844.1, 361.6] (w=108.7 h=44.2) — same parked car but
    the bbox extended 35px to the right (curb shadow / adjacent vehicle
    clipping). IoU is 0.53 — below the 0.65 idle gate — so the strict
    IoU loop rejects the merge. _try_idle_iom_match must catch it
    because intersection-over-min ≈ 0.99 and area ratio is 1.86 (<2.0).

    Without this rescue, the jittered detection spawns a phantom track
    on top of the idle car and fires a duplicate vehicle_idle 150 s
    later."""
    monkeypatch.setenv("CAMERA_ID", "cam1")
    import importlib
    from services.tracker.core import manager as mgr
    importlib.reload(mgr)

    fake = FakeRedis()
    m = mgr.Manager(fake)
    m._process_vehicle_detections(
        [{"bbox": [735, 324, 810, 358], "class_name": "car",
          "confidence": 0.8}],
        timestamp=0.0,
    )
    assert len(m.tracked_vehicles) == 1
    parked = next(iter(m.tracked_vehicles.values()))
    parked.idle_alerted = True
    original_id = parked.vehicle_id

    # Same parked car, jittered wider bbox. IoU = 0.53, IoM = 0.99.
    m._process_vehicle_detections(
        [{"bbox": [735, 317, 844, 362], "class_name": "car",
          "confidence": 0.75}],
        timestamp=1.0,
    )
    assert len(m.tracked_vehicles) == 1, "jittered bbox must absorb into idle track"
    assert next(iter(m.tracked_vehicles)) == original_id, \
        "track id must be preserved, not re-issued"


def test_idle_iom_rescue_rejects_person_inside_truck_bbox(monkeypatch):
    """Negative case: a parked truck's bbox fully contains a pedestrian's
    bbox (IoM = 1.0 from the person's perspective). The IoM rescue MUST
    NOT fire — area ratio is ~25× and a `person` class isn't compatible
    with `car/truck/bus`. Without these guards, IoM=1.0 would always
    win and the tracker would silently merge unrelated detections."""
    monkeypatch.setenv("CAMERA_ID", "cam1")
    import importlib
    from services.tracker.core import manager as mgr
    importlib.reload(mgr)

    fake = FakeRedis()
    m = mgr.Manager(fake)
    # parked truck — large bbox
    m._process_vehicle_detections(
        [{"bbox": [200, 200, 600, 500], "class_name": "truck",
          "confidence": 0.8}],
        timestamp=0.0,
    )
    parked = next(iter(m.tracked_vehicles.values()))
    parked.idle_alerted = True

    # pedestrian-shaped bbox fully inside the truck bbox — but as a
    # 'motorcycle' (since vehicle-detector wouldn't emit 'person'). Area
    # ratio = (400*300) / (40*100) = 30×. Class 'motorcycle' is also not
    # in the car/truck/bus equiv set, so _class_compatible returns False.
    iom_match = m._try_idle_iom_match(
        bbox=[300, 250, 340, 350], class_name="motorcycle",
    )
    assert iom_match is None, \
        "IoM rescue must reject when class mismatched OR area ratio too large"


def test_idle_iom_rescue_skips_non_idle_tracks(monkeypatch):
    """A drive-by car briefly overlapping another moving car's bbox must
    NOT merge via the IoM rescue — the rescue is for idle/stationary
    tracks only. Without the gate, two moving cars with overlapping
    bboxes (e.g., trailing car at intersection) would merge."""
    monkeypatch.setenv("CAMERA_ID", "cam1")
    import importlib
    from services.tracker.core import manager as mgr
    importlib.reload(mgr)

    fake = FakeRedis()
    m = mgr.Manager(fake)
    m._process_vehicle_detections(
        [{"bbox": [100, 100, 200, 200], "class_name": "car",
          "confidence": 0.8}],
        timestamp=0.0,
    )
    # NOT marked idle — still moving.
    assert not next(iter(m.tracked_vehicles.values())).idle_alerted

    # Smaller bbox fully inside, IoM=1.0, area ratio 4× (still <... wait,
    # we want to test the idle gate specifically). Use area ratio 1.5 so
    # ONLY the idle gate gates it out.
    result = m._try_idle_iom_match(bbox=[110, 110, 200, 200], class_name="car")
    assert result is None, "IoM rescue must skip non-idle tracks"


def test_idle_iom_rescue_respects_class_compatibility_for_4wheel(monkeypatch):
    """Positive case for #41 interaction — YOLO flips class car↔truck on
    the same physical parked vehicle AND outputs a jittered bbox.
    Without _class_compatible(), the IoM rescue would reject the truck
    detection against the car track even though it's the same car."""
    monkeypatch.setenv("CAMERA_ID", "cam1")
    import importlib
    from services.tracker.core import manager as mgr
    importlib.reload(mgr)

    fake = FakeRedis()
    m = mgr.Manager(fake)
    m._process_vehicle_detections(
        [{"bbox": [735, 324, 810, 358], "class_name": "car",
          "confidence": 0.8}],
        timestamp=0.0,
    )
    parked = next(iter(m.tracked_vehicles.values()))
    parked.idle_alerted = True

    # Same parked vehicle, jittered bbox, YOLO flipped to truck.
    m._process_vehicle_detections(
        [{"bbox": [735, 317, 844, 362], "class_name": "truck",
          "confidence": 0.7}],
        timestamp=1.0,
    )
    assert len(m.tracked_vehicles) == 1, \
        "car↔truck flip + jittered bbox on idle must still absorb"


def test_idle_confirmed_track_survives_long_detector_gap(monkeypatch):
    """Contract: a parked car (idle_alerted=True) that the detector briefly
    misses for > VEHICLE_GHOST_TTL but < VEHICLE_IDLE_GHOST_TTL must NOT
    fire vehicle_gone — it must re-attach to the same track_id when the
    detector re-finds it. Repeated short-gap misses on the same parked
    car were spawning a new vehicle_NNNN every gap; observed live on cam1
    where one parked car produced 4 separate tracks over 17 min."""
    monkeypatch.setenv("CAMERA_ID", "cam1")
    import importlib
    from services.tracker.core import manager as mgr
    importlib.reload(mgr)
    from services.tracker.core.config import (
        VEHICLE_LOST_TIMEOUT, VEHICLE_GHOST_TTL, VEHICLE_IDLE_GHOST_TTL,
    )
    assert VEHICLE_IDLE_GHOST_TTL > VEHICLE_GHOST_TTL, \
        "idle ghost TTL must exceed the regular ghost TTL"

    fake = FakeRedis()
    m = mgr.Manager(fake)
    bbox = [100, 100, 200, 200]

    # Park the car: detected at t=0, then mark idle_alerted=True directly
    # (skips waiting VEHICLE_IDLE_TIMEOUT 90s — the test is about ghost
    # expiry, not the idle-detection threshold).
    m._process_vehicle_detections(
        [{"bbox": bbox, "class_name": "car", "confidence": 0.85}],
        timestamp=0.0,
    )
    veh = next(iter(m.tracked_vehicles.values()))
    veh.idle_alerted = True

    # Detector misses the car: track goes stale → ghosted at LOST_TIMEOUT+
    stale_t = VEHICLE_LOST_TIMEOUT + 1.0
    m._process_vehicle_detections([], timestamp=stale_t)
    assert veh.vehicle_id in m._ghost_vehicles, \
        "track should be in ghost buffer after LOST_TIMEOUT"

    # Time advances past the OLD ghost TTL (30 s) but still well within
    # the IDLE ghost TTL (600 s default). Track must NOT be expired.
    mid_t = stale_t + VEHICLE_GHOST_TTL + 30.0  # 30 s past old TTL
    assert mid_t < stale_t + VEHICLE_IDLE_GHOST_TTL
    m._process_vehicle_detections([], timestamp=mid_t)
    assert veh.vehicle_id in m._ghost_vehicles, \
        "idle ghost survived past old GHOST_TTL — bug is the regression we're guarding against"

    # Past the IDLE ghost TTL: now it expires + fires vehicle_gone + vehicle_left.
    expire_t = stale_t + VEHICLE_IDLE_GHOST_TTL + 1.0
    m._process_vehicle_detections([], timestamp=expire_t)
    assert veh.vehicle_id not in m._ghost_vehicles

    events = [fields for _id, fields in fake._streams.get("events:cam1", [])]
    gone = [e for e in events if e.get("event_type") == "vehicle_gone"]
    left = [e for e in events if e.get("event_type") == "vehicle_left"]
    assert len(gone) == 1, f"expected exactly 1 vehicle_gone at idle expiry: {events}"
    assert len(left) == 1, f"idle-alerted track must emit vehicle_left at expiry: {events}"


def test_non_idle_track_still_uses_regular_ghost_ttl(monkeypatch):
    """Contract regression-guard: bumping the idle ghost TTL must NOT also
    extend the regular ghost TTL for drive-by tracks. A drive-by track
    (idle_alerted=False) must still fire vehicle_gone at the old 30 s
    window, not the new 600 s window."""
    monkeypatch.setenv("CAMERA_ID", "cam1")
    import importlib
    from services.tracker.core import manager as mgr
    importlib.reload(mgr)
    from services.tracker.core.config import (
        VEHICLE_LOST_TIMEOUT, VEHICLE_GHOST_TTL,
    )

    fake = FakeRedis()
    m = mgr.Manager(fake)
    m._process_vehicle_detections(
        [{"bbox": [100, 100, 200, 200], "class_name": "car", "confidence": 0.85}],
        timestamp=0.0,
    )
    veh = next(iter(m.tracked_vehicles.values()))
    assert veh.idle_alerted is False

    stale_t = VEHICLE_LOST_TIMEOUT + 1.0
    m._process_vehicle_detections([], timestamp=stale_t)
    expire_t = stale_t + VEHICLE_GHOST_TTL + 1.0
    m._process_vehicle_detections([], timestamp=expire_t)

    events = [fields for _id, fields in fake._streams.get("events:cam1", [])]
    gone = [e for e in events if e.get("event_type") == "vehicle_gone"]
    assert len(gone) == 1
    assert gone[0]["was_idle"] == "False"


def test_yolo_class_flicker_car_to_truck_keeps_single_track(monkeypatch):
    """Contract: YOLOv8 frequently classifies a single physical vehicle
    as 'car' in one frame and 'truck' in the next (esp. pickups at angle
    change). The tracker's ghost-match + live-center-match fallbacks
    used to require strict class equality, which produced two separate
    tracks for one physical drive-by. Observed live on cam1: red pickup
    at 11:35 → vehicle_0009 (car) + vehicle_0010 (truck) 2 s apart.

    After this fix, _class_compatible treats car/truck/bus as
    interchangeable; ghost-match + center-match re-associate across the
    flicker so the same physical pickup keeps one track id."""
    monkeypatch.setenv("CAMERA_ID", "cam1")
    import importlib
    from services.tracker.core import manager as mgr
    importlib.reload(mgr)
    from services.tracker.core.config import VEHICLE_LOST_TIMEOUT

    fake = FakeRedis()
    m = mgr.Manager(fake)

    # Frame 1: pickup detected at left of frame, YOLO says "car"
    m._process_vehicle_detections(
        [{"bbox": [400, 300, 550, 380], "class_name": "car",
          "confidence": 0.78}],
        timestamp=0.0,
    )
    assert len(m.tracked_vehicles) == 1
    original_vid = next(iter(m.tracked_vehicles))

    # Pickup keeps moving but detector misses it for >LOST_TIMEOUT,
    # so the track goes to ghost.
    stale_t = VEHICLE_LOST_TIMEOUT + 1.0
    m._process_vehicle_detections([], timestamp=stale_t)
    assert original_vid in m._ghost_vehicles

    # Frame N: same pickup re-detected slightly to the right, but YOLO
    # NOW classifies it as "truck" (the bug-trigger). Strict-class
    # ghost-match would refuse to re-associate. With the relaxed
    # compatibility, ghost-match accepts it and the SAME track id is
    # reused.
    m._process_vehicle_detections(
        [{"bbox": [490, 295, 640, 380], "class_name": "truck",
          "confidence": 0.81}],
        timestamp=stale_t + 1.0,
    )
    assert original_vid in m.tracked_vehicles, \
        "car↔truck flicker must keep the same track id"
    assert len(m.tracked_vehicles) == 1


def test_yolo_class_flicker_car_to_bus_also_keeps_single_track(monkeypatch):
    """Same contract as the car↔truck case but for car↔bus. Long vans /
    box trucks frequently flip between these classes."""
    monkeypatch.setenv("CAMERA_ID", "cam1")
    import importlib
    from services.tracker.core import manager as mgr
    importlib.reload(mgr)
    from services.tracker.core.config import VEHICLE_LOST_TIMEOUT

    fake = FakeRedis()
    m = mgr.Manager(fake)
    m._process_vehicle_detections(
        [{"bbox": [400, 300, 550, 380], "class_name": "car",
          "confidence": 0.7}],
        timestamp=0.0,
    )
    original_vid = next(iter(m.tracked_vehicles))
    stale_t = VEHICLE_LOST_TIMEOUT + 1.0
    m._process_vehicle_detections([], timestamp=stale_t)
    m._process_vehicle_detections(
        [{"bbox": [490, 295, 640, 380], "class_name": "bus",
          "confidence": 0.7}],
        timestamp=stale_t + 1.0,
    )
    assert original_vid in m.tracked_vehicles, \
        "car↔bus flicker must keep the same track id"
    assert len(m.tracked_vehicles) == 1


def test_motorcycle_does_not_inherit_car_track(monkeypatch):
    """Contract regression-guard: the class-compatibility relaxation
    must NOT extend to motorcycle/bicycle. YOLO almost never confuses
    those for 4-wheel vehicles, and a passing motorcycle inheriting a
    car's track id would be a real data error. Strict class equality
    still applies for these classes."""
    monkeypatch.setenv("CAMERA_ID", "cam1")
    import importlib
    from services.tracker.core import manager as mgr
    importlib.reload(mgr)
    from services.tracker.core.config import VEHICLE_LOST_TIMEOUT

    fake = FakeRedis()
    m = mgr.Manager(fake)
    m._process_vehicle_detections(
        [{"bbox": [400, 300, 550, 380], "class_name": "car",
          "confidence": 0.85}],
        timestamp=0.0,
    )
    original_vid = next(iter(m.tracked_vehicles))
    stale_t = VEHICLE_LOST_TIMEOUT + 1.0
    m._process_vehicle_detections([], timestamp=stale_t)
    # Motorcycle at almost the same spot — must mint a NEW track, not
    # ghost-match into the car's id.
    m._process_vehicle_detections(
        [{"bbox": [490, 295, 640, 380], "class_name": "motorcycle",
          "confidence": 0.85}],
        timestamp=stale_t + 1.0,
    )
    moto_track = next(
        (vid for vid in m.tracked_vehicles if vid != original_vid),
        None,
    )
    assert moto_track is not None, \
        "motorcycle must NOT inherit the car's track id"


def test_brief_track_with_no_follow_up_traffic_still_expires(monkeypatch):
    """Contract: `_process_vehicle_detections` must be called even on
    empty-detection messages (and on empty xreadgroup polls) so the ghost
    sweep can fire `vehicle_gone` for terminated tracks. Before the fix,
    `main.py` skipped the call when `detections` was empty — a single-frame
    drive-by on an otherwise quiet camera left the vehicle stuck in
    `tracked_vehicles` forever and the vehicle-attributes flush never ran.
    """
    monkeypatch.setenv("CAMERA_ID", "cam1")
    import importlib
    from services.tracker.core import manager as mgr
    importlib.reload(mgr)
    from services.tracker.core.config import (
        VEHICLE_LOST_TIMEOUT, VEHICLE_GHOST_TTL,
    )

    fake = FakeRedis()
    m = mgr.Manager(fake)

    # One-frame drive-by — the actual symptom on cam1.
    m._process_vehicle_detections(
        [{"bbox": [100, 100, 200, 200], "class_name": "car",
          "confidence": 0.85}],
        timestamp=0.0,
    )
    assert len(m.tracked_vehicles) == 1

    # The fix: main.py now calls this on every poll, even with no new
    # detections. Each empty call lets the sweep run.
    stale_t = VEHICLE_LOST_TIMEOUT + 1.0
    m._process_vehicle_detections([], timestamp=stale_t)
    expire_t = stale_t + VEHICLE_GHOST_TTL + 1.0
    m._process_vehicle_detections([], timestamp=expire_t)

    assert len(m.tracked_vehicles) == 0, \
        "tracked_vehicles must be drained after ghost expiry"
    events = [fields for _id, fields in fake._streams.get("events:cam1", [])]
    gone = [e for e in events if e.get("event_type") == "vehicle_gone"]
    assert len(gone) == 1, (
        "vehicle_gone must fire for a brief drive-by even without follow-up "
        "traffic — that's the trigger vehicle-attributes uses to flush the "
        f"per-track dir. got events: {events}"
    )


def test_idle_leave_emits_both_vehicle_gone_and_vehicle_left(monkeypatch):
    """A car that went idle then left should emit BOTH vehicle_gone
    (internal — attribute service flush trigger) AND vehicle_left (user-
    facing idle-leave notification)."""
    monkeypatch.setenv("CAMERA_ID", "cam1")
    import importlib
    from services.tracker.core import manager as mgr
    importlib.reload(mgr)
    from services.tracker.core.config import (
        VEHICLE_LOST_TIMEOUT, VEHICLE_IDLE_GHOST_TTL,
    )

    fake = FakeRedis()
    m = mgr.Manager(fake)
    # Force the vehicle into idle_alerted=True state by directly setting the
    # flag after a normal detection. Going through the natural is_stationary
    # path would require feeding 5+ same-position frames + crossing the
    # idle_timeout — slow and unrelated to what this test asserts. The
    # tracker's idle-detection path has its own tests; we just need a
    # TrackedVehicle with idle_alerted=True to verify the emit branching.
    bbox = [100, 100, 200, 200]
    m._process_vehicle_detections(
        [{"bbox": bbox, "class_name": "car", "confidence": 0.8}],
        timestamp=0.0,
    )
    # Mark the one tracked vehicle as having gone idle.
    veh = next(iter(m.tracked_vehicles.values()))
    veh.idle_alerted = True

    # First sweep at LOST_TIMEOUT+ moves the vehicle into the ghost buffer.
    # Idle-alerted tracks use VEHICLE_IDLE_GHOST_TTL (default 600 s) not the
    # regular 30 s — see `_process_vehicle_detections` step 2b.
    stale_t = VEHICLE_LOST_TIMEOUT + 1.0
    m._process_vehicle_detections([], timestamp=stale_t)
    expire_t = stale_t + VEHICLE_IDLE_GHOST_TTL + 1.0
    m._process_vehicle_detections([], timestamp=expire_t)

    events = [fields for _id, fields in fake._streams.get("events:cam1", [])]
    gone = [e for e in events if e.get("event_type") == "vehicle_gone"]
    left = [e for e in events if e.get("event_type") == "vehicle_left"]

    assert len(gone) == 1
    assert gone[0]["was_idle"] == "True"
    assert len(left) == 1, (
        f"expected 1 vehicle_left for idle-leave, got {len(left)}: {events}"
    )

