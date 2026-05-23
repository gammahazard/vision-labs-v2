"""Unit tests for cameras.py:_validate_camera dependency rules."""
import pytest
from services.dashboard import cameras


def _valid_base():
    return {
        "id": "cam1",
        "name": "Front",
        "rtsp_sub": "rtsp://192.0.2.1/sub",
    }


def test_validate_rejects_attrs_without_vehicles():
    entry = _valid_base()
    entry["detect_vehicles"] = False
    entry["detect_vehicle_attributes"] = True
    err = cameras._validate_camera(entry)
    assert err is not None
    assert "detect_vehicle_attributes" in err
    assert "detect_vehicles" in err


def test_validate_accepts_attrs_with_vehicles():
    entry = _valid_base()
    entry["detect_vehicles"] = True
    entry["detect_vehicle_attributes"] = True
    assert cameras._validate_camera(entry) is None


def test_validate_accepts_attrs_unset():
    """Existing cameras (no detect_vehicle_attributes key) still validate."""
    entry = _valid_base()
    entry["detect_vehicles"] = True
    assert cameras._validate_camera(entry) is None


def test_validate_accepts_attrs_false_with_vehicles_false():
    entry = _valid_base()
    entry["detect_vehicles"] = False
    entry["detect_vehicle_attributes"] = False
    assert cameras._validate_camera(entry) is None


# ---------------------------------------------------------------------------
# Auto-enable classifier on detect_vehicle_attributes flip
# ---------------------------------------------------------------------------
class TestAutoEnableClassifier:
    """When any camera transitions detect_vehicle_attributes False→True,
    cameras.upsert_camera should write ENABLE_CLASSIFIER=1 to .env and
    publish config:apply for vehicle-attributes (orchestrator expands to
    all enabled slots).

    Without this, the per-cam toggle in the dashboard UI starts the
    vehicle-attributes-camN container but the classifier silently stays
    disabled because ENABLE_CLASSIFIER defaults to 0. metadata.json
    comes back with attributes=null and users have no clue why.
    """

    def _setup_fakes(self, monkeypatch, existing=None):
        """Wire fakes for ctx (Redis), update_env, logger. Returns a
        dict with captured `published` messages and `env_writes`."""
        from services.dashboard import cameras as cam_mod
        from services.dashboard.routes import __init__ as ctx_mod  # noqa: F401

        published = []
        env_writes = []

        class FakeRedis:
            def __init__(self):
                self._h = {}
                if existing is not None:
                    import json
                    self._h.setdefault("cameras:registry", {})["cam1"] = json.dumps(existing)
            def hget(self, k, f):
                v = self._h.get(k, {}).get(f)
                return v
            def hgetall(self, k):
                return self._h.get(k, {})
            def hset(self, k, *a, **kw):
                if len(a) >= 2:
                    self._h.setdefault(k, {})[a[0]] = a[1]
                return 1
            def hdel(self, k, f):
                self._h.get(k, {}).pop(f, None)
                return 1
            def publish(self, channel, msg):
                published.append((channel, msg))
                return 1

        fake_r = FakeRedis()
        monkeypatch.setattr(cam_mod.ctx, "r", fake_r, raising=False)
        # REGISTRY_KEY lives on cameras module itself, not ctx; the constant
        # is already "cameras:registry" so no patch needed.

        # Mock update_env so we capture without touching .env on the host
        import sys
        fake_env_writer = type(sys)("helpers.env_writer")
        def fake_update_env(updates, path=None):
            env_writes.append(updates)
            return {"ok": True, "written": list(updates.keys()), "ignored": [], "error": None}
        fake_env_writer.update_env = fake_update_env
        sys.modules["helpers.env_writer"] = fake_env_writer

        return {"published": published, "env_writes": env_writes}

    def test_auto_enable_on_false_to_true_transition(self, monkeypatch):
        """User toggles detect_vehicle_attributes from False to True on
        an existing camera — ENABLE_CLASSIFIER=1 must be written + config:apply
        published for vehicle-attributes."""
        from services.dashboard import cameras
        existing = {
            "id": "cam1",
            "name": "Front",
            "rtsp_sub": "rtsp://192.0.2.1/sub",
            "detect_persons": True,
            "detect_vehicles": True,
            "detect_faces": True,
            "detect_vehicle_attributes": False,
        }
        caps = self._setup_fakes(monkeypatch, existing=existing)

        new_entry = dict(existing)
        new_entry["detect_vehicle_attributes"] = True
        ok, err = cameras.upsert_camera(new_entry)

        assert ok and err is None, f"upsert failed: {err}"
        # ENABLE_CLASSIFIER written
        assert any("ENABLE_CLASSIFIER" in w for w in caps["env_writes"]), \
            f"ENABLE_CLASSIFIER not written. env_writes={caps['env_writes']}"
        # config:apply published for vehicle-attributes (gets expanded by orchestrator)
        va_publishes = [
            m for ch, m in caps["published"]
            if ch == "config:apply" and "vehicle-attributes" in m and "ENABLE_CLASSIFIER" in m
        ]
        assert va_publishes, \
            f"no config:apply for ENABLE_CLASSIFIER. published={caps['published']}"

    def test_no_auto_enable_when_unchanged(self, monkeypatch):
        """Re-upserting with the same detect_vehicle_attributes value
        does NOT write ENABLE_CLASSIFIER (idempotency — don't churn
        containers on every save)."""
        from services.dashboard import cameras
        existing = {
            "id": "cam1",
            "name": "Front",
            "rtsp_sub": "rtsp://192.0.2.1/sub",
            "detect_persons": True,
            "detect_vehicles": True,
            "detect_faces": True,
            "detect_vehicle_attributes": True,
        }
        caps = self._setup_fakes(monkeypatch, existing=existing)

        # Same value — no change
        new_entry = dict(existing)
        ok, err = cameras.upsert_camera(new_entry)

        assert ok, f"upsert failed: {err}"
        assert not caps["env_writes"], \
            f"unexpected env writes on idempotent upsert: {caps['env_writes']}"

    def test_no_auto_enable_on_true_to_false(self, monkeypatch):
        """When the LAST camera toggles detect_vehicle_attributes back
        to False, ENABLE_CLASSIFIER is intentionally NOT auto-disabled.
        Rationale: the container exits cleanly and frees VRAM regardless
        of the env value, and a user who manually set ENABLE_CLASSIFIER=1
        to test should not have it silently flipped back."""
        from services.dashboard import cameras
        existing = {
            "id": "cam1",
            "name": "Front",
            "rtsp_sub": "rtsp://192.0.2.1/sub",
            "detect_persons": True,
            "detect_vehicles": True,
            "detect_faces": True,
            "detect_vehicle_attributes": True,
        }
        caps = self._setup_fakes(monkeypatch, existing=existing)

        new_entry = dict(existing)
        new_entry["detect_vehicle_attributes"] = False
        ok, err = cameras.upsert_camera(new_entry)

        assert ok, f"upsert failed: {err}"
        # No ENABLE_CLASSIFIER write (would be downgrade)
        assert not any("ENABLE_CLASSIFIER" in w for w in caps["env_writes"]), \
            f"unexpected ENABLE_CLASSIFIER write on disable: {caps['env_writes']}"

    def test_new_camera_with_attrs_true_auto_enables(self, monkeypatch):
        """A camera created with detect_vehicle_attributes=true from
        the start (no prior existing entry) also triggers the
        auto-enable. Covers API-style creates."""
        from services.dashboard import cameras
        caps = self._setup_fakes(monkeypatch, existing=None)

        new_entry = {
            "id": "cam1",
            "name": "Front",
            "rtsp_sub": "rtsp://192.0.2.1/sub",
            "detect_persons": True,
            "detect_vehicles": True,
            "detect_faces": True,
            "detect_vehicle_attributes": True,
        }
        ok, err = cameras.upsert_camera(new_entry)

        assert ok, f"upsert failed: {err}"
        assert any("ENABLE_CLASSIFIER" in w for w in caps["env_writes"]), \
            f"ENABLE_CLASSIFIER not written on new-camera path. env_writes={caps['env_writes']}"
