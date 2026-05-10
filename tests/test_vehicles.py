"""
tests/test_vehicles.py — Real tests for vehicle detection pipeline.

Tests the full vehicle flow: tracker event emission, dashboard event API
returning vehicle-specific fields, browse API for day/snapshot listing,
and path traversal protection.

NO real Redis or GPU — tracker logic and routes are tested with FakeRedis.
"""

import os
import sys
import json
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
    def xrevrange(self, name, count=None, *args, **kwargs):
        stream = self._streams.get(name, [])
        result = list(reversed(stream))
        if count:
            result = result[:count]
        return result

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
def setup_routes(fake_redis):
    """Set up the routes context module with fake Redis."""
    import routes as ctx
    ctx.r = fake_redis
    ctx.logger = __import__("logging").getLogger("test_vehicles")
    ctx.FACE_API_URL = "http://localhost:8081"
    ctx.EVENT_STREAM = "events:test_cam"
    ctx.FRAME_STREAM = "frames:test_cam"
    ctx.DETECTION_STREAM = "detections:pose:test_cam"
    ctx.STATE_KEY = "state:test_cam"
    ctx.CONFIG_KEY = "config:pipeline"
    ctx.IDENTITY_KEY = "identity_state:test_cam"
    ctx.ZONE_KEY = "zones:test_cam"
    ctx.AUTH_DB_PATH = ""
    ctx.DEFAULT_CONFIG = {
        "confidence_thresh": "0.6",
        "iou_threshold": "0.45",
        "lost_timeout": "5",
        "target_fps": "8",
    }
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
    """Verify GET /api/events returns vehicle-specific fields."""

    def test_vehicle_event_has_vehicle_class(self, event_client, fake_redis, setup_routes):
        """vehicle_detected events must include vehicle_class in API response."""
        client, _ = event_client
        fake_redis._streams[setup_routes.EVENT_STREAM] = [
            ("5000-0", {
                "event_type": "vehicle_detected",
                "person_id": "",
                "timestamp": "1708000000",
                "vehicle_class": "truck",
                "vehicle_confidence": "0.87",
                "snapshot_key": "vehicle_snapshot:cam1:1708000000",
                "camera_id": "test_cam",
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
        fake_redis._streams[setup_routes.EVENT_STREAM] = [
            ("3000-0", {
                "event_type": "person_appeared",
                "person_id": "p42",
                "timestamp": "1708000000",
                "action": "standing",
                "camera_id": "test_cam",
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
        fake_redis._streams[setup_routes.EVENT_STREAM] = [
            ("1000-0", {
                "event_type": "person_appeared",
                "person_id": "p1",
                "timestamp": "1000",
                "camera_id": "test_cam",
            }),
            ("2000-0", {
                "event_type": "vehicle_detected",
                "person_id": "",
                "timestamp": "2000",
                "vehicle_class": "car",
                "vehicle_confidence": "0.95",
                "snapshot_key": "vehicle_snapshot:cam1:2000",
                "camera_id": "test_cam",
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
        """Lists snapshots for a given day with parsed time and class."""
        client, vehicle_dir = browse_client

        day = vehicle_dir / "2025-02-20"
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
        assert snaps[0]["url"] == "/api/browse/snapshot/2025-02-20/15-00-10_motorcycle.jpg"
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
        event_key = stream_key(EVENT_STREAM, camera_id="front_door")
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
        assert data["camera_id"] == "front_door"

    def test_vehicle_event_rate_limiting(self, tracker_instance):
        """Only one vehicle_detected event emitted per VEHICLE_RATE_LIMIT_SEC."""
        tracker, r = tracker_instance

        # Two different vehicles at different positions (won't IoU match)
        det1 = [{"bbox": [100, 200, 300, 400], "class_name": "truck", "confidence": 0.85}]
        det2 = [{"bbox": [500, 200, 700, 400], "class_name": "car", "confidence": 0.70}]

        # First call — should emit vehicle_detected
        tracker._process_vehicle_detections(det1, 1708000000.0)
        # Second call immediately with different vehicle — rate-limited
        tracker._process_vehicle_detections(det2, 1708000001.0)

        detected_events = [e for _, e in self._get_events(r) if e["event_type"] == "vehicle_detected"]
        assert len(detected_events) == 1  # Only one, second was rate-limited

    def test_vehicle_snapshot_stored_in_redis(self, tracker_instance):
        """Vehicle snapshot bytes are stored in Redis with snapshot key."""
        tracker, r = tracker_instance

        jpeg = b"\xff\xd8\xff\xe0real_snapshot_data"
        detections = [{
            "bbox": [10, 20, 200, 300],
            "class_name": "bus",
            "confidence": 0.77,
        }]

        tracker._process_vehicle_detections(detections, 1708000000.0, jpeg)

        # Snapshot should be stored
        snap_key = "vehicle_snapshot:front_door:1708000000"
        assert r._keys.get(snap_key) == jpeg

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
        det_both = [
            {"bbox": [10, 100, 150, 250], "class_name": "car", "confidence": 0.9},
            {"bbox": [400, 100, 550, 250], "class_name": "truck", "confidence": 0.85},
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

        # Different vehicle at t=15 (original is stale after 10s)
        det2 = [{"bbox": [500, 200, 700, 400], "class_name": "bus", "confidence": 0.7}]
        tracker._process_vehicle_detections(det2, 1708000015.0)

        # Original vehicle should be pruned, only new one remains
        assert len(tracker.tracked_vehicles) == 1
        remaining = list(tracker.tracked_vehicles.values())[0]
        assert remaining.class_name == "bus"


# ===========================================================================
# TrackedVehicle — is_stationary, center_history, snapshot_bbox
# ===========================================================================
class TestTrackedVehicleStationary:
    """Unit tests for TrackedVehicle.is_stationary and related state."""

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
            veh.update([100, 200, 300, 400], 0.9, float(i))
        assert len(veh.center_history) == 10
        assert veh.is_stationary is True

    def test_not_stationary_when_moving(self):
        """Vehicle shifting 50px per frame → is_stationary == False."""
        veh = self._make_vehicle()
        for i in range(1, 10):
            # Shift x1,x2 by 50px each frame
            shifted_bbox = [100 + i * 50, 200, 300 + i * 50, 400]
            veh.update(shifted_bbox, 0.9, float(i))
        assert veh.is_stationary is False

    def test_not_stationary_with_few_frames(self):
        """< 5 frames → always False (not enough data)."""
        veh = self._make_vehicle()
        # Only 1 center from __init__, add 2 more = 3 total
        veh.update([100, 200, 300, 400], 0.9, 1.0)
        veh.update([100, 200, 300, 400], 0.9, 2.0)
        assert len(veh.center_history) == 3
        assert veh.is_stationary is False

    def test_stationary_at_exactly_5_frames(self):
        """Exactly 5 frames at same position → is_stationary == True."""
        veh = self._make_vehicle()
        for i in range(1, 5):  # 1 from init + 4 = 5 total
            veh.update([100, 200, 300, 400], 0.9, float(i))
        assert len(veh.center_history) == 5
        assert veh.is_stationary is True

    def test_center_history_capped_at_20(self):
        """After 25 updates, center_history should be capped at 20."""
        veh = self._make_vehicle()
        for i in range(1, 26):  # 1 from init + 25 = 26 attempts
            veh.update([100, 200, 300, 400], 0.9, float(i))
        assert len(veh.center_history) == 20

    def test_stationary_boundary_29px_is_stationary(self):
        """Center drift of exactly 29px → is_stationary == True (< 30px)."""
        veh = self._make_vehicle(bbox=[100, 200, 300, 400])
        # Center starts at (200, 300). Shift bbox so center drifts by 29px
        # horizontally: new center = (229, 300) → drift = 29px
        for i in range(1, 6):
            veh.update([100, 200, 300, 400], 0.9, float(i))
        # Final frame: shift center by 29px
        veh.update([129, 200, 329, 400], 0.9, 6.0)
        assert veh.is_stationary is True

    def test_stationary_boundary_31px_is_not_stationary(self):
        """Center drift of 31px → is_stationary == False (>= 30px)."""
        veh = self._make_vehicle(bbox=[100, 200, 300, 400])
        for i in range(1, 6):
            veh.update([100, 200, 300, 400], 0.9, float(i))
        # Final frame: shift center by 31px
        veh.update([131, 200, 331, 400], 0.9, 6.0)
        assert veh.is_stationary is False

    def test_snapshot_bbox_preserved_after_update(self):
        """snapshot_bbox should stay at initial bbox even after updates."""
        veh = self._make_vehicle(bbox=[100, 200, 300, 400])
        # snapshot_bbox is set to initial bbox
        assert veh.snapshot_bbox == [100, 200, 300, 400]
        # Update with different bbox
        veh.update([110, 210, 310, 410], 0.9, 1.0)
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
        event_key = stream_key(EVENT_STREAM, camera_id="front_door")
        events = bfr._streams.get(event_key, [])
        event_types = [e["event_type"] for _, e in events]
        assert "vehicle_idle" not in event_types  # Moving → no idle

