"""
tests/test_routes.py — Tests for dashboard API routes.

Tests the dashboard's REST API endpoints using FastAPI's TestClient
with a mocked Redis backend. Each route module is tested independently.

Coverage:
  - Zones:         CRUD + validation (alert levels, min points, dead_zone)
  - Config:        GET/POST config + whitelist filtering + stats
  - Events:        List events + snapshot serving
  - Auth:          Login, logout, password change, status
  - Notifications: Status endpoint + configuration check

NO real Redis — all Redis calls are mocked via a FakeRedis dict-store.
"""

import os
import sys
import json
import time
import tempfile
import pytest

# ---------------------------------------------------------------------------
# Path setup — mirror the service's import structure
# ---------------------------------------------------------------------------
# We need dashboard code + contracts on the path
_DASHBOARD_DIR = os.path.join(
    os.path.dirname(__file__), "..", "services", "dashboard"
)
_CONTRACTS_DIR = os.path.join(os.path.dirname(__file__), "..", "contracts")
sys.path.insert(0, _DASHBOARD_DIR)
sys.path.insert(0, _CONTRACTS_DIR)


# ---------------------------------------------------------------------------
# FakeRedis — lightweight dict-based mock that satisfies route usage
# ---------------------------------------------------------------------------
class FakeRedis:
    """Minimal Redis mock supporting hash, stream, and key operations."""

    def __init__(self):
        self._hashes = {}   # key → {field: value}
        self._streams = {}  # key → [(id, data)]
        self._keys = {}     # key → value (for get/set)
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

    def xadd(self, name, fields):
        if name not in self._streams:
            self._streams[name] = []
        stream_id = f"{int(time.time() * 1000)}-{len(self._streams[name])}"
        self._streams[name].append((stream_id, fields))
        return stream_id

    # --- Key ops ---
    def get(self, name):
        return self._keys.get(name)

    def set(self, name, value):
        self._keys[name] = value

    def setex(self, name, ttl, value):
        self._keys[name] = value


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_redis():
    return FakeRedis()


@pytest.fixture
def setup_routes(fake_redis):
    """Set up the routes context module with fake Redis and return the app."""
    import routes as ctx
    ctx.r = fake_redis
    ctx.logger = __import__("logging").getLogger("test")
    ctx.FACE_API_URL = "http://localhost:8081"
    ctx.EVENT_STREAM = "events:test_cam"
    ctx.FRAME_STREAM = "frames:test_cam"
    ctx.DETECTION_STREAM = "detections:pose:test_cam"
    ctx.STATE_KEY = "state:test_cam"
    ctx.CONFIG_KEY = "config:pipeline"
    ctx.IDENTITY_KEY = "identity_state:test_cam"
    ctx.ZONE_KEY = "zones:test_cam"
    ctx.AUTH_DB_PATH = ""  # Set per-test if needed

    ctx.DEFAULT_CONFIG = {
        "confidence_thresh": "0.6",
        "iou_threshold": "0.45",
        "lost_timeout": "5",
        "target_fps": "8",
    }

    return ctx


@pytest.fixture
def zone_client(setup_routes):
    """FastAPI TestClient for zone routes."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.zones import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.fixture
def config_client(setup_routes):
    """FastAPI TestClient for config routes."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.config import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.fixture
def event_client(setup_routes, tmp_path):
    """FastAPI TestClient for event routes."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    # Patch SNAPSHOT_DIR to a temp directory
    import routes.events as events_mod
    events_mod.SNAPSHOT_DIR = str(tmp_path)
    from routes.events import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app), tmp_path


@pytest.fixture
def auth_client(setup_routes, tmp_path):
    """FastAPI TestClient for auth routes with temp database."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routes as ctx

    # Use temp file for auth DB
    db_path = str(tmp_path / "test_auth.db")
    ctx.AUTH_DB_PATH = db_path

    # Reset the cached DB path in auth module
    import routes.auth as auth_mod
    auth_mod._DB_PATH = None
    auth_mod._SECRET_KEY = None
    auth_mod.init_auth_db()

    from routes.auth import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# ===========================================================================
# Zone Route Tests
# ===========================================================================
class TestZoneRoutes:
    """Tests for /api/zones endpoints."""

    def test_list_zones_empty(self, zone_client):
        resp = zone_client.get("/api/zones")
        assert resp.status_code == 200
        assert resp.json()["zones"] == []

    def test_create_zone(self, zone_client):
        zone = {
            "name": "Front Yard",
            "points": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]],
            "alert_level": "always",
        }
        resp = zone_client.post("/api/zones", json=zone)
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Front Yard"
        assert data["alert_level"] == "always"
        assert "id" in data
        assert data["id"].startswith("zone_")

    def test_create_zone_appears_in_list(self, zone_client):
        zone = {
            "name": "Driveway",
            "points": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]],
            "alert_level": "night_only",
        }
        zone_client.post("/api/zones", json=zone)
        resp = zone_client.get("/api/zones")
        zones = resp.json()["zones"]
        assert len(zones) == 1
        assert zones[0]["name"] == "Driveway"

    def test_create_zone_too_few_points(self, zone_client):
        zone = {
            "name": "Bad Zone",
            "points": [[0, 0], [1, 1]],  # Only 2 points
            "alert_level": "always",
        }
        resp = zone_client.post("/api/zones", json=zone)
        assert resp.status_code == 400
        assert "3 points" in resp.json()["error"]

    def test_create_zone_invalid_alert_level_defaults_to_log_only(self, zone_client):
        zone = {
            "name": "Test",
            "points": [[0, 0], [1, 0], [1, 1]],
            "alert_level": "invalid_level",
        }
        resp = zone_client.post("/api/zones", json=zone)
        assert resp.status_code == 200
        assert resp.json()["alert_level"] == "log_only"

    def test_create_zone_dead_zone_accepted(self, zone_client):
        """Regression: dead_zone was previously not in the validation allowlist."""
        zone = {
            "name": "Dead Zone",
            "points": [[0, 0], [1, 0], [1, 1]],
            "alert_level": "dead_zone",
        }
        resp = zone_client.post("/api/zones", json=zone)
        assert resp.status_code == 200
        assert resp.json()["alert_level"] == "dead_zone"

    def test_update_zone(self, zone_client):
        # Create first
        zone = {
            "name": "Original",
            "points": [[0, 0], [1, 0], [1, 1]],
            "alert_level": "always",
        }
        create_resp = zone_client.post("/api/zones", json=zone)
        zone_id = create_resp.json()["id"]

        # Update name
        update_resp = zone_client.put(
            f"/api/zones/{zone_id}",
            json={"name": "Updated Name"},
        )
        assert update_resp.status_code == 200
        assert update_resp.json()["name"] == "Updated Name"
        assert update_resp.json()["alert_level"] == "always"  # Unchanged

    def test_update_zone_points(self, zone_client):
        zone = {
            "name": "Test",
            "points": [[0, 0], [1, 0], [1, 1]],
            "alert_level": "always",
        }
        create_resp = zone_client.post("/api/zones", json=zone)
        zone_id = create_resp.json()["id"]

        new_points = [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]]
        update_resp = zone_client.put(
            f"/api/zones/{zone_id}",
            json={"points": new_points},
        )
        assert update_resp.status_code == 200
        assert len(update_resp.json()["points"]) == 4

    def test_update_zone_too_few_points(self, zone_client):
        zone = {
            "name": "Test",
            "points": [[0, 0], [1, 0], [1, 1]],
            "alert_level": "always",
        }
        zone_id = zone_client.post("/api/zones", json=zone).json()["id"]

        resp = zone_client.put(
            f"/api/zones/{zone_id}",
            json={"points": [[0, 0], [1, 1]]},
        )
        assert resp.status_code == 400

    def test_update_nonexistent_zone(self, zone_client):
        resp = zone_client.put(
            "/api/zones/zone_doesnt_exist",
            json={"name": "Nope"},
        )
        assert resp.status_code == 404

    def test_delete_zone(self, zone_client):
        zone = {
            "name": "To Delete",
            "points": [[0, 0], [1, 0], [1, 1]],
            "alert_level": "log_only",
        }
        zone_id = zone_client.post("/api/zones", json=zone).json()["id"]

        resp = zone_client.delete(f"/api/zones/{zone_id}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

        # Verify gone
        list_resp = zone_client.get("/api/zones")
        assert len(list_resp.json()["zones"]) == 0

    def test_delete_nonexistent_zone(self, zone_client):
        resp = zone_client.delete("/api/zones/zone_fake")
        assert resp.status_code == 404

    def test_all_valid_alert_levels(self, zone_client):
        """Every valid alert level should be accepted without fallback."""
        for level in ("always", "night_only", "log_only", "ignore", "dead_zone"):
            zone = {
                "name": f"Zone {level}",
                "points": [[0, 0], [1, 0], [1, 1]],
                "alert_level": level,
            }
            resp = zone_client.post("/api/zones", json=zone)
            assert resp.json()["alert_level"] == level, f"Level {level} not accepted"


# ===========================================================================
# Config Route Tests
# ===========================================================================
class TestConfigRoutes:
    """Tests for /api/config and /api/stats endpoints."""

    def test_get_config_returns_defaults_when_empty(self, config_client, setup_routes):
        resp = config_client.get("/api/config")
        assert resp.status_code == 200
        config = resp.json()["config"]
        assert config == setup_routes.DEFAULT_CONFIG

    def test_get_config_returns_stored_values(self, config_client, fake_redis, setup_routes):
        fake_redis.hset(
            setup_routes.CONFIG_KEY,
            mapping={"confidence_thresh": "0.8", "target_fps": "15"},
        )
        resp = config_client.get("/api/config")
        assert resp.json()["config"]["confidence_thresh"] == "0.8"
        assert resp.json()["config"]["target_fps"] == "15"

    def test_post_config_updates_allowed_keys(self, config_client, setup_routes):
        resp = config_client.post("/api/config", json={
            "confidence_thresh": "0.75",
            "target_fps": "12",
        })
        assert resp.status_code == 200
        config = resp.json()["config"]
        assert config["confidence_thresh"] == "0.75"
        assert config["target_fps"] == "12"

    def test_post_config_rejects_unknown_keys(self, config_client, fake_redis, setup_routes):
        config_client.post("/api/config", json={
            "confidence_thresh": "0.5",
            "evil_key": "hack",
            "another_bad": "value",
        })
        stored = fake_redis.hgetall(setup_routes.CONFIG_KEY)
        assert "evil_key" not in stored
        assert "another_bad" not in stored
        assert stored.get("confidence_thresh") == "0.5"

    def test_stats_returns_stream_lengths(self, config_client, fake_redis, setup_routes):
        # Add some fake data to streams
        fake_redis._streams[setup_routes.FRAME_STREAM] = [("1-0", {}), ("2-0", {})]
        fake_redis._streams[setup_routes.EVENT_STREAM] = [("1-0", {})]

        resp = config_client.get("/api/stats")
        assert resp.status_code == 200
        stats = resp.json()
        assert stats["frames_in_stream"] == 2
        assert stats["events_in_stream"] == 1


# ===========================================================================
# Event Route Tests
# ===========================================================================
class TestEventRoutes:
    """Tests for /api/events and /api/events/{id}/snapshot endpoints."""

    def test_list_events_empty(self, event_client):
        client, _ = event_client
        resp = client.get("/api/events")
        assert resp.status_code == 200
        assert resp.json()["events"] == []

    def test_list_events_returns_data(self, event_client, fake_redis, setup_routes):
        client, _ = event_client
        fake_redis._streams[setup_routes.EVENT_STREAM] = [
            ("1000-0", {
                "event_type": "person_appeared",
                "person_id": "p1",
                "timestamp": "1000",
                "action": "standing",
                "camera_id": "test_cam",
            }),
            ("2000-0", {
                "event_type": "person_left",
                "person_id": "p1",
                "timestamp": "2000",
                "duration": "60",
                "direction": "left",
                "camera_id": "test_cam",
            }),
        ]
        resp = client.get("/api/events?count=10")
        events = resp.json()["events"]
        assert len(events) == 2
        # xrevrange returns newest first
        assert events[0]["event_type"] == "person_left"
        assert events[1]["event_type"] == "person_appeared"

    def test_snapshot_serves_image(self, event_client):
        client, tmp_path = event_client
        # Create a fake snapshot file
        event_id = "1000-0"
        safe_id = event_id.replace(":", "-")
        snapshot_path = tmp_path / f"{safe_id}.jpg"
        snapshot_path.write_bytes(b"\xff\xd8\xff\xe0fake_jpeg_data")

        resp = client.get(f"/api/events/{event_id}/snapshot")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"
        assert b"fake_jpeg_data" in resp.content

    def test_snapshot_not_found(self, event_client):
        client, _ = event_client
        resp = client.get("/api/events/999-0/snapshot")
        assert resp.status_code == 404


# ===========================================================================
# Auth Route Tests
# ===========================================================================
class TestAuthRoutes:
    """Tests for /api/auth/* endpoints."""

    def test_default_admin_login(self, auth_client):
        resp = auth_client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin"},
        )
        assert resp.status_code == 200
        assert resp.json()["username"] == "admin"
        # Should set a session cookie
        assert "vl_session" in resp.cookies

    def test_login_wrong_password(self, auth_client):
        resp = auth_client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "wrong"},
        )
        assert resp.status_code == 401

    def test_login_nonexistent_user(self, auth_client):
        resp = auth_client.post(
            "/api/auth/login",
            json={"username": "nobody", "password": "test"},
        )
        assert resp.status_code == 401

    def test_auth_status_not_logged_in(self, auth_client):
        resp = auth_client.get("/api/auth/status")
        assert resp.status_code == 200
        assert resp.json()["logged_in"] is False

    def test_auth_status_logged_in(self, auth_client):
        # Login first
        login_resp = auth_client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin"},
        )
        session_cookie = login_resp.cookies.get("vl_session")

        # Check status with cookie
        auth_client.cookies.set("vl_session", session_cookie)
        resp = auth_client.get("/api/auth/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["logged_in"] is True
        assert data["username"] == "admin"

    def test_logout(self, auth_client):
        # Login
        login_resp = auth_client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin"},
        )
        auth_client.cookies.set("vl_session", login_resp.cookies.get("vl_session"))

        # Logout
        logout_resp = auth_client.post("/api/auth/logout")
        assert logout_resp.status_code == 200

    def test_change_password(self, auth_client):
        # Login
        login_resp = auth_client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin"},
        )
        auth_client.cookies.set("vl_session", login_resp.cookies.get("vl_session"))

        # Change password
        change_resp = auth_client.post(
            "/api/auth/change-password",
            json={"current_password": "admin", "new_password": "newpass123"},
        )
        assert change_resp.status_code == 200

        # Login with new password
        auth_client.cookies.clear()
        new_login = auth_client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "newpass123"},
        )
        assert new_login.status_code == 200

    def test_change_password_wrong_current(self, auth_client):
        login_resp = auth_client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin"},
        )
        auth_client.cookies.set("vl_session", login_resp.cookies.get("vl_session"))

        resp = auth_client.post(
            "/api/auth/change-password",
            json={"current_password": "wrong", "new_password": "newpass"},
        )
        assert resp.status_code == 401


# ===========================================================================
# Notification Route Tests
# ===========================================================================
class TestNotificationRoutes:
    """Tests for /api/notifications endpoints."""

    @pytest.fixture
    def notif_client(self, setup_routes):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routes.notifications import router
        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_notification_status_unconfigured(self, notif_client, monkeypatch):
        """When no bot token is set, status should report unconfigured."""
        import routes.notifications as notif_mod
        monkeypatch.setattr(notif_mod, "TELEGRAM_BOT_TOKEN", "")
        monkeypatch.setattr(notif_mod, "TELEGRAM_CHAT_ID", "")

        resp = notif_client.get("/api/notifications/status")
        assert resp.status_code == 200
        assert resp.json()["configured"] is False

    def test_notification_status_configured(self, notif_client, monkeypatch):
        """When bot token and chat ID are set, status should report configured."""
        import routes.notifications as notif_mod
        monkeypatch.setattr(notif_mod, "TELEGRAM_BOT_TOKEN", "12345:ABC")
        monkeypatch.setattr(notif_mod, "TELEGRAM_CHAT_ID", "67890")

        resp = notif_client.get("/api/notifications/status")
        assert resp.status_code == 200
        assert resp.json()["configured"] is True


# ===========================================================================
# server.py IoU Helper Tests
# ===========================================================================
def _bbox_iou(box_a, box_b):
    """IoU between two [x1, y1, x2, y2] boxes — mirrors server.py implementation."""
    xa = max(box_a[0], box_b[0])
    ya = max(box_a[1], box_b[1])
    xb = min(box_a[2], box_b[2])
    yb = min(box_a[3], box_b[3])
    inter = max(0, xb - xa) * max(0, yb - ya)
    if inter == 0:
        return 0.0
    area_a = (box_a[2] - box_a[0]) * (box_a[3] - box_a[1])
    area_b = (box_b[2] - box_b[0]) * (box_b[3] - box_b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


class TestBboxIou:
    """Test the _bbox_iou helper (mirrors server.py implementation)."""

    def test_identical_boxes(self):
        assert _bbox_iou([0, 0, 100, 100], [0, 0, 100, 100]) == 1.0

    def test_no_overlap(self):
        assert _bbox_iou([0, 0, 50, 50], [60, 60, 100, 100]) == 0.0

    def test_partial_overlap(self):
        iou = _bbox_iou([0, 0, 100, 100], [50, 50, 150, 150])
        # Intersection: 50x50=2500, Union: 10000+10000-2500=17500
        assert abs(iou - 2500 / 17500) < 0.001

    def test_one_inside_other(self):
        iou = _bbox_iou([0, 0, 100, 100], [25, 25, 75, 75])
        # Intersection: 50x50=2500, Union: 10000+2500-2500=10000
        assert abs(iou - 0.25) < 0.001

    def test_zero_area_box(self):
        assert _bbox_iou([0, 0, 0, 0], [0, 0, 100, 100]) == 0.0
