# Vehicle Attributes — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship per-track grouping of vehicle snapshots — every drive-by + idle car produces a directory with multi-angle HD crops + metadata.json, surfaced as grouped cards in Browse. **No classifier in this phase** (Phase 3 ships the ML model).

**Architecture:** Tracker emits a new `vehicle_sample` event every 3rd matched-vehicle update. A new per-cam service `vehicle-attributes-cam{N}` (one container per camera profile) subscribes to `events:{cam}`, maintains per-track HD-crop buffers, and on `vehicle_left` / `vehicle_idle` writes the buffer to `/data/snapshots/vehicles/{cam}/{date}/{track_id}/`. UI gets a new `detect_vehicle_attributes` flag (hard-depends on `detect_vehicles`) in setup wizard + add-camera + edit-camera modal. Browse UI renders grouped cards.

**Tech Stack:** Python 3.11, Redis (streams + pub/sub + binary GET on HD frames), OpenCV-Python (cropping + JPEG encode only — no model inference yet), FastAPI (dashboard routes), vanilla JS (browse UI).

**Spec:** `docs/superpowers/specs/2026-05-21-vehicle-attribute-classification-design.md` §1.3, §2.1–2.5, §5 Phase 1, §9.

---

## File map

**Create:**
- `services/vehicle-attributes/Dockerfile` — slim Python image, no GPU
- `services/vehicle-attributes/requirements.txt` — redis + opencv-headless + Pillow
- `services/vehicle-attributes/service.py` — entrypoint, registry gate, event loop
- `services/vehicle-attributes/buffer.py` — `TrackBuffer` dataclass + per-track crop accumulation
- `services/vehicle-attributes/storage.py` — per-track filesystem layout writer
- `tests/test_vehicle_attributes_buffer.py` — unit tests for buffer
- `tests/test_vehicle_attributes_storage.py` — unit tests for storage writer
- `tests/test_vehicle_attributes_service.py` — service-level integration with FakeRedis

**Modify:**
- `contracts/streams.py` — add `VEHICLE_SAMPLE_EVENT` constant
- `services/tracker/core/manager.py:357-431` — add `_emit_vehicle_sample_event`, wire into update path at L258-283
- `services/tracker/core/manager.py` (top of file) — read `EMIT_VEHICLE_SAMPLES` + `SAMPLE_INTERVAL_FRAMES` env vars
- `services/dashboard/cameras.py:107-151` — extend `_validate_camera` with `detect_vehicle_attributes` + dep on `detect_vehicles`
- `services/dashboard/cameras.py:332` — extend `detector_to_service` map in `upsert_camera`
- `services/orchestrator/orchestrator.py:124-149` — add `"vehicle-attributes"` to `CONFIG_APPLY_ALLOWED_SERVICES` + `PER_CAM_SERVICE_PREFIXES`
- `docker-compose.yml` — 20 new `vehicle-attributes-camN` blocks + `EMIT_VEHICLE_SAMPLES` env on every tracker-camN
- `services/dashboard/static/setup.html:177` — new `detectVehicleAttributes` checkbox with `data-requires="detectVehicles"`
- `services/dashboard/static/cameras.html` (add-camera form ~L154, edit modal ~L235) — two new checkboxes
- `services/dashboard/static/js/pages/cameras.js:361-363, openEditModal, handleSaveEdit` — wire new field
- `services/dashboard/static/js/pages/setup.js` (apply-config + state collection) — wire new field
- `services/dashboard/routes/browse.py` — new endpoint `GET /api/browse/tracks/{date}` for grouped layout
- `services/dashboard/static/js/dashboard/browse.js` — render grouped cards
- `services/dashboard/static/css/style.css` — track-card styles
- `tests/test_vehicles.py` — new test for tracker `vehicle_sample` emission
- `tests/test_orchestrator.py` — extend tests for the new prefix
- `CHANGELOG.md` — `[Unreleased] → Added` entry
- `CONTEXT.md` §4 — add §4.x service inventory entry for vehicle-attributes

---

## Task 0: Branch + scaffold

**Files:** none (git ops only)

- [ ] **Step 0.1: Create the feature branch**

```bash
git checkout main && git pull
git checkout -b feat/vehicle-attributes-phase-1
```

- [ ] **Step 0.2: Verify tests are green at the starting point**

Run: `source .venv-test/bin/activate && pytest -q`
Expected: `388 passed`

---

## Task 1: Schema additions

**Files:**
- Modify: `contracts/streams.py`

The contracts module is bind-mounted into every service so a constant added here is picked up on next restart of any consumer; no rebuild needed for services that aren't the producer.

- [ ] **Step 1.1: Add the new event-type constant**

Open `contracts/streams.py` and locate the `EVENT_STREAM` definition (around line 46). Add directly below it:

```python
# Per-track sampling trigger emitted by the tracker on matched vehicle updates.
# Phase 1 of the vehicle-attributes pipeline — consumed by
# `vehicle-attributes-cam{N}` to know when to crop the current HD frame.
# Schema-additive: pre-existing consumers ignore unknown `event_type` values.
# Payload mirrors `vehicle_detected` (same XADD into EVENT_STREAM, different
# `event_type` field). Gated by tracker env `EMIT_VEHICLE_SAMPLES` (default
# false) + `SAMPLE_INTERVAL_FRAMES` (default 3). See spec §2.2.
VEHICLE_SAMPLE_EVENT = "vehicle_sample"
```

- [ ] **Step 1.2: Commit**

```bash
git add contracts/streams.py
git commit -m "contracts: add VEHICLE_SAMPLE_EVENT constant for tracker→attributes pipeline"
```

---

## Task 2: Tracker emits `vehicle_sample` (TDD)

**Files:**
- Test: `tests/test_vehicles.py`
- Modify: `services/tracker/core/manager.py`

- [ ] **Step 2.1: Write the failing test**

Open `tests/test_vehicles.py` and append (or place near other `_emit_vehicle_*` tests):

```python
def test_tracker_emits_vehicle_sample_every_n_updates(monkeypatch):
    """With EMIT_VEHICLE_SAMPLES=1 and SAMPLE_INTERVAL_FRAMES=3, the third
    matched update on an existing vehicle emits `vehicle_sample`. The 1st
    and 2nd matched updates do not."""
    monkeypatch.setenv("EMIT_VEHICLE_SAMPLES", "1")
    monkeypatch.setenv("SAMPLE_INTERVAL_FRAMES", "3")
    monkeypatch.setenv("CAMERA_ID", "cam1")
    import importlib
    from services.tracker.core import manager as mgr
    importlib.reload(mgr)

    fake = FakeRedis()
    m = mgr.Manager(fake)

    bbox = [100, 100, 200, 200]
    m._process_vehicle_detections([{"bbox": bbox, "class_name": "car",
                                    "confidence": 0.8}], timestamp=0.0)
    m._process_vehicle_detections([{"bbox": bbox, "class_name": "car",
                                    "confidence": 0.8}], timestamp=1.0)
    m._process_vehicle_detections([{"bbox": bbox, "class_name": "car",
                                    "confidence": 0.8}], timestamp=2.0)
    m._process_vehicle_detections([{"bbox": bbox, "class_name": "car",
                                    "confidence": 0.8}], timestamp=3.0)

    events = [fields for _id, fields in fake._streams.get("events:cam1", [])]
    sample_events = [e for e in events if e.get("event_type") == "vehicle_sample"]
    detected_events = [e for e in events if e.get("event_type") == "vehicle_detected"]

    assert len(detected_events) == 1
    assert len(sample_events) == 1
    s = sample_events[0]
    assert s["vehicle_id"].startswith("vehicle_")
    assert json.loads(s["bbox"]) == bbox


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
```

- [ ] **Step 2.2: Run the test, confirm it fails**

Run: `pytest tests/test_vehicles.py::test_tracker_emits_vehicle_sample_every_n_updates -v`
Expected: FAIL — `len(sample_events) == 0` because the emit-method doesn't exist yet.

- [ ] **Step 2.3: Read env at module top of manager.py**

Open `services/tracker/core/manager.py`. At the top of the module (near other `os.getenv(...)` reads), add:

```python
# Phase 1 of the vehicle-attributes pipeline. Off by default until the
# consumer (vehicle-attributes-cam{N}) is wired up. See spec §2.2.
EMIT_VEHICLE_SAMPLES = os.getenv("EMIT_VEHICLE_SAMPLES", "0") == "1"
SAMPLE_INTERVAL_FRAMES = max(1, int(os.getenv("SAMPLE_INTERVAL_FRAMES", "3")))
```

- [ ] **Step 2.4: Add the emit method**

In the same file, directly below `_emit_vehicle_detected_event` (around line 391), add:

```python
def _emit_vehicle_sample_event(self, veh: 'TrackedVehicle', timestamp: float):
    """Emit a low-weight sampling event the attribute service uses to
    decide when to crop the current HD frame for this track.

    Mirrors vehicle_detected payload so a consumer can treat both as
    `(track_id, bbox, timestamp)` carriers without branching on event_type.
    """
    event = {
        "camera_id": CAMERA_ID,
        "event_type": VEHICLE_SAMPLE_EVENT,
        "timestamp": str(timestamp),
        "bbox": json.dumps(veh.bbox),
        "vehicle_class": veh.class_name,
        "vehicle_confidence": str(round(veh.confidence, 3)),
        "vehicle_id": veh.vehicle_id,
        "vehicle_first_seen": str(int(veh.first_seen)),
        "frame_count": str(veh.frame_count),
    }
    self.r.xadd(EVENT_STREAM, event, maxlen=MAX_EVENT_STREAM_LEN)
```

Also extend the existing `from contracts.streams import ...` to add `VEHICLE_SAMPLE_EVENT`.

- [ ] **Step 2.5: Wire the sample-emit into matched-vehicle update**

In the same file, locate the matched-vehicle branch around line 258. The `veh.update(...)` call at L261 increments `veh.frame_count` automatically (`state.py:51`). Add the gate **after** the idle-check block at L283, before the closing of the `if best_match_id:` branch:

```python
                # Phase 1: emit a sampling event every Nth matched update
                # so vehicle-attributes-cam{N} can pull the HD frame at a
                # known cadence. Cheap pubsub-equivalent — costs one XADD.
                if EMIT_VEHICLE_SAMPLES and veh.frame_count % SAMPLE_INTERVAL_FRAMES == 0:
                    self._emit_vehicle_sample_event(veh, timestamp)
```

- [ ] **Step 2.6: Run the new tests, confirm both pass**

Run: `pytest tests/test_vehicles.py::test_tracker_emits_vehicle_sample_every_n_updates tests/test_vehicles.py::test_tracker_does_not_emit_sample_when_feature_disabled -v`
Expected: 2 PASS.

- [ ] **Step 2.7: Run full test suite to confirm no regressions**

Run: `pytest -q`
Expected: All passing (was 388, now 390).

- [ ] **Step 2.8: Commit**

```bash
git add contracts/streams.py services/tracker/core/manager.py tests/test_vehicles.py
git commit -m "tracker: emit vehicle_sample every Nth matched update (gated, off by default)"
```

---

## Task 3: Registry flag + dependency

**Files:**
- Modify: `services/dashboard/cameras.py:107-151` (`_validate_camera`)
- Modify: `services/dashboard/cameras.py:332` (`upsert_camera` detector→service map)
- Test: `tests/test_cameras_validation.py` (create if missing)

- [ ] **Step 3.1: Write failing tests for validation**

Write to `tests/test_cameras_validation.py`:

```python
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
```

- [ ] **Step 3.2: Run, confirm 1 failing test**

Run: `pytest tests/test_cameras_validation.py -v`
Expected: FAIL on `test_validate_rejects_attrs_without_vehicles` — `_validate_camera` accepts the inconsistent combination (returns None).

- [ ] **Step 3.3: Extend `_validate_camera`**

In `services/dashboard/cameras.py`, locate the existing `detect_faces → detect_persons` block (around line 149-150):

```python
    if entry.get("detect_faces") and entry.get("detect_persons") is False:
        return "'detect_faces' requires 'detect_persons' to be true"
```

Add directly below it:

```python
    # Same shape as detect_faces dependency. The vehicle-attributes service
    # can't classify what the vehicle detector never sees.
    if entry.get("detect_vehicle_attributes") and entry.get("detect_vehicles") is False:
        return "'detect_vehicle_attributes' requires 'detect_vehicles' to be true"
```

- [ ] **Step 3.4: Run the validation tests, confirm 4 PASS**

Run: `pytest tests/test_cameras_validation.py -v`
Expected: 4 PASS.

- [ ] **Step 3.5: Extend `upsert_camera`'s detector→service map**

In the same file, locate the `detector_to_service` dict in `upsert_camera` (added by PR #19, around line 332):

```python
if existing:
    detector_to_service = {
        "detect_persons":  f"pose-detector-{cid}",
        "detect_vehicles": f"vehicle-detector-{cid}",
        "detect_faces":    f"face-recognizer-{cid}",
    }
```

Add the fourth entry:

```python
if existing:
    detector_to_service = {
        "detect_persons":            f"pose-detector-{cid}",
        "detect_vehicles":           f"vehicle-detector-{cid}",
        "detect_faces":              f"face-recognizer-{cid}",
        "detect_vehicle_attributes": f"vehicle-attributes-{cid}",
    }
```

- [ ] **Step 3.6: Run pytest broadly to confirm no regressions**

Run: `pytest -q`
Expected: All passing (now 394).

- [ ] **Step 3.7: Commit**

```bash
git add services/dashboard/cameras.py tests/test_cameras_validation.py
git commit -m "cameras: detect_vehicle_attributes flag with hard dep on detect_vehicles"
```

---

## Task 4: Orchestrator allowlist

**Files:**
- Modify: `services/orchestrator/orchestrator.py:124-149`
- Test: `tests/test_orchestrator.py`

- [ ] **Step 4.1: Write the failing tests**

Open `tests/test_orchestrator.py`. Append in the `TestExpandPerCamServices` class:

```python
    def test_vehicle_attributes_bare_name_expands_to_enabled_cams(
        self, fake_redis, restrict_profiles
    ):
        """`vehicle-attributes` is a new per-cam service prefix added in
        Phase 1 of the attribute classifier work. Bare-name expansion must
        work the same way as `vehicle-detector` etc."""
        fake_redis._hashes[orchestrator.REGISTRY_KEY] = {
            "cam1": json.dumps({"id": "cam1", "enabled": True}),
            "cam2": json.dumps({"id": "cam2", "enabled": True}),
        }
        expanded, profiles = orchestrator._expand_per_cam_services(
            fake_redis, ["vehicle-attributes"],
        )
        assert "vehicle-attributes-cam1" in expanded
        assert "vehicle-attributes-cam2" in expanded
        assert "vehicle-attributes" not in expanded
        assert set(profiles) == {"cam1", "cam2"}

    def test_vehicle_attributes_pre_expanded_passes_through(
        self, fake_redis, restrict_profiles
    ):
        """The detector-flag toggle path (cameras.py:upsert_camera) publishes
        pre-expanded `vehicle-attributes-cam2` for per-camera changes."""
        fake_redis._hashes[orchestrator.REGISTRY_KEY] = {
            "cam2": json.dumps({"id": "cam2", "enabled": True}),
        }
        expanded, profiles = orchestrator._expand_per_cam_services(
            fake_redis, ["vehicle-attributes-cam2"],
        )
        assert expanded == ["vehicle-attributes-cam2"]
        assert profiles == ["cam2"]
```

- [ ] **Step 4.2: Run, confirm both fail**

Run: `pytest tests/test_orchestrator.py::TestExpandPerCamServices::test_vehicle_attributes_bare_name_expands_to_enabled_cams -v`
Expected: FAIL — bare name not recognized as a per-cam prefix yet.

- [ ] **Step 4.3: Add `vehicle-attributes` to the allowlist + prefix set**

In `services/orchestrator/orchestrator.py`, locate `CONFIG_APPLY_ALLOWED_SERVICES` around line 124. Add `"vehicle-attributes"`:

```python
CONFIG_APPLY_ALLOWED_SERVICES = {
    "pose-detector", "vehicle-detector", "face-recognizer",
    "camera-ingester", "ollama", "dashboard",
    "recorder",
    "tracker",
    "grafana",
    # Phase 1 of the vehicle-attributes pipeline. Per-cam profile-gated
    # service; same per-cam-expansion contract as the other detectors.
    "vehicle-attributes",
}
```

Then in `PER_CAM_SERVICE_PREFIXES` around line 146, add it:

```python
PER_CAM_SERVICE_PREFIXES = {
    "recorder", "pose-detector", "vehicle-detector",
    "face-recognizer", "camera-ingester", "tracker",
    "vehicle-attributes",
}
```

- [ ] **Step 4.4: Run the new tests, confirm both pass**

Run: `pytest tests/test_orchestrator.py::TestExpandPerCamServices::test_vehicle_attributes_bare_name_expands_to_enabled_cams tests/test_orchestrator.py::TestExpandPerCamServices::test_vehicle_attributes_pre_expanded_passes_through -v`
Expected: 2 PASS.

- [ ] **Step 4.5: Run full orchestrator class to confirm no regression**

Run: `pytest tests/test_orchestrator.py -q`
Expected: 73 passing (was 71, +2).

- [ ] **Step 4.6: Commit**

```bash
git add services/orchestrator/orchestrator.py tests/test_orchestrator.py
git commit -m "orchestrator: allowlist vehicle-attributes as new per-cam service prefix"
```

---

## Task 5: New service — Dockerfile + requirements

**Files:**
- Create: `services/vehicle-attributes/Dockerfile`
- Create: `services/vehicle-attributes/requirements.txt`

- [ ] **Step 5.1: Confirm the directory doesn't exist yet**

Run: `ls services/vehicle-attributes/ 2>&1`
Expected: `ls: cannot access … No such file or directory`.

- [ ] **Step 5.2: Create requirements.txt**

Write to `services/vehicle-attributes/requirements.txt`:

```
redis==5.3.1
opencv-python-headless==4.13.0.88
Pillow==10.4.0
numpy==1.26.4
```

- [ ] **Step 5.3: Create Dockerfile**

Write to `services/vehicle-attributes/Dockerfile`:

```dockerfile
# services/vehicle-attributes/Dockerfile
#
# PURPOSE:
#   Per-camera vehicle attribute pipeline (Phase 1: capture + group only,
#   no classifier yet). Consumes events:{cam}, maintains per-track HD-crop
#   buffers, writes /data/snapshots/vehicles/{cam}/{date}/{track_id}/.
#
# NO GPU IN PHASE 1.
#   Cropping + JPEG encode is CPU work. We deliberately use a slim
#   python:3.11 base (not vision-labs-base:cuda12.8) to keep the image
#   under 200 MB. Phase 3 will swap this base when the classifier ships.

FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY service.py buffer.py storage.py ./

RUN mkdir -p /data

CMD ["python", "-u", "service.py"]
```

- [ ] **Step 5.4: Commit**

```bash
git add services/vehicle-attributes/Dockerfile services/vehicle-attributes/requirements.txt
git commit -m "vehicle-attributes: Dockerfile + pinned deps (no GPU in phase 1)"
```

---

## Task 6: `TrackBuffer` dataclass (TDD)

**Files:**
- Create: `services/vehicle-attributes/buffer.py`
- Create: `services/vehicle_attributes/__init__.py` (pytest import alias)
- Create: `services/vehicle_attributes/buffer.py` (shim)
- Test: `tests/test_vehicle_attributes_buffer.py`

The service directory is `services/vehicle-attributes/` (dashed, matches other per-cam services). Python imports can't use dashes — we create an underscore-named shim package that only pytest sees (Docker COPYs from the dashed dir directly).

- [ ] **Step 6.1: Write the failing tests**

Write to `tests/test_vehicle_attributes_buffer.py`:

```python
"""Unit tests for vehicle-attributes TrackBuffer."""
import pytest
from services.vehicle_attributes.buffer import TrackBuffer


def test_buffer_starts_empty():
    b = TrackBuffer(track_id="vehicle_0042", camera_id="cam1",
                    first_seen=1000.0)
    assert b.crops == []
    assert b.confidences == []
    assert b.bboxes == []
    assert b.is_full() is False
    assert b.hero_index() is None


def test_buffer_append_records_all_three_lists():
    b = TrackBuffer(track_id="vehicle_0042", camera_id="cam1",
                    first_seen=1000.0)
    b.append(crop=b"\xff\xd8jpegA", yolo_conf=0.85, bbox=[10, 10, 50, 50])
    assert b.crops == [b"\xff\xd8jpegA"]
    assert b.confidences == [0.85]
    assert b.bboxes == [[10, 10, 50, 50]]


def test_buffer_caps_at_max_crops():
    b = TrackBuffer(track_id="v", camera_id="cam1", first_seen=0.0,
                    max_crops=3)
    for i in range(5):
        b.append(crop=bytes([i]), yolo_conf=0.5, bbox=[0, 0, 1, 1])
    assert len(b.crops) == 3
    # First-N policy: drive-bys show broadest angle coverage early.
    assert b.crops == [bytes([0]), bytes([1]), bytes([2])]
    assert b.is_full() is True


def test_buffer_hero_index_picks_highest_confidence():
    b = TrackBuffer(track_id="v", camera_id="cam1", first_seen=0.0)
    b.append(crop=b"a", yolo_conf=0.6, bbox=[0, 0, 1, 1])
    b.append(crop=b"b", yolo_conf=0.9, bbox=[0, 0, 1, 1])
    b.append(crop=b"c", yolo_conf=0.75, bbox=[0, 0, 1, 1])
    assert b.hero_index() == 1


def test_buffer_hero_index_none_when_empty():
    b = TrackBuffer(track_id="v", camera_id="cam1", first_seen=0.0)
    assert b.hero_index() is None
```

- [ ] **Step 6.2: Run, confirm import error**

Run: `pytest tests/test_vehicle_attributes_buffer.py -v`
Expected: collection error — `ModuleNotFoundError`.

- [ ] **Step 6.3: Create the underscore-named pytest shim package**

Write to `services/vehicle_attributes/__init__.py`:

```python
"""Pytest-only import alias for the dashed service dir.

The Dockerfile COPYs `services/vehicle-attributes/*.py` directly; this
underscore-prefixed shim only exists so pytest can `import
services.vehicle_attributes.buffer`. Both files point at the same source.
"""
from pathlib import Path
import sys

_dashed = Path(__file__).resolve().parent.parent / "vehicle-attributes"
if _dashed.is_dir():
    sys.path.insert(0, str(_dashed))
```

Write to `services/vehicle_attributes/buffer.py`:

```python
"""Re-export shim — actual source lives in ../vehicle-attributes/buffer.py."""
from buffer import *  # noqa: F401,F403  (pulls from sys.path inserted in __init__)
```

(Same shim pattern repeated for storage.py and service.py in later tasks.)

- [ ] **Step 6.4: Implement the dataclass**

Write to `services/vehicle-attributes/buffer.py`:

```python
"""Per-track HD-crop buffer for vehicle-attributes Phase 1.

The buffer accumulates HD JPEG crops keyed by track_id as the tracker emits
`vehicle_sample` events. On `vehicle_left` or `vehicle_idle` the buffer is
flushed to disk by `storage.py`.

Phase 1 caps the buffer at 8 crops (spec §2.3). Phase 3 will run a classifier
across the buffered crops with weighted majority voting.
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TrackBuffer:
    track_id: str
    camera_id: str
    first_seen: float
    crops: list[bytes] = field(default_factory=list)
    confidences: list[float] = field(default_factory=list)
    bboxes: list[list[int]] = field(default_factory=list)
    last_sampled_at: float = 0.0
    max_crops: int = 8

    def append(self, crop: bytes, yolo_conf: float, bbox: list[int]) -> None:
        """Add a crop. Silently drops if already at max_crops.

        First-N policy (not LRU): drive-bys typically show the most angle
        diversity in the first few frames as the car enters the frame.
        """
        if self.is_full():
            return
        self.crops.append(crop)
        self.confidences.append(yolo_conf)
        self.bboxes.append(list(bbox))

    def is_full(self) -> bool:
        return len(self.crops) >= self.max_crops

    def hero_index(self) -> Optional[int]:
        if not self.confidences:
            return None
        return max(range(len(self.confidences)),
                   key=lambda i: self.confidences[i])
```

- [ ] **Step 6.5: Run buffer tests, confirm 5 PASS**

Run: `pytest tests/test_vehicle_attributes_buffer.py -v`
Expected: 5 PASS.

- [ ] **Step 6.6: Commit**

```bash
git add services/vehicle-attributes/buffer.py services/vehicle_attributes/__init__.py services/vehicle_attributes/buffer.py tests/test_vehicle_attributes_buffer.py
git commit -m "vehicle-attributes: TrackBuffer dataclass + pytest import shim"
```

---

## Task 7: Storage writer (TDD)

**Files:**
- Create: `services/vehicle-attributes/storage.py`
- Create: `services/vehicle_attributes/storage.py` (shim)
- Test: `tests/test_vehicle_attributes_storage.py`

- [ ] **Step 7.1: Write the failing tests**

Write to `tests/test_vehicle_attributes_storage.py`:

```python
"""Unit tests for vehicle-attributes storage writer."""
import json
from pathlib import Path
import pytest
from services.vehicle_attributes.buffer import TrackBuffer
from services.vehicle_attributes.storage import flush_buffer_to_disk


def _seeded_buffer(n_crops=3):
    b = TrackBuffer(track_id="vehicle_0042", camera_id="cam1",
                    first_seen=1779394901.5)
    for i in range(n_crops):
        b.append(crop=bytes([0xFF, 0xD8]) + f"crop_{i}".encode(),
                 yolo_conf=0.5 + i * 0.1,
                 bbox=[10 + i, 20 + i, 50 + i, 60 + i])
    return b


def test_flush_creates_per_track_directory(tmp_path):
    b = _seeded_buffer(3)
    flush_buffer_to_disk(b, last_seen=1779394907.2, event_kind="drive_by",
                         vehicle_class="car",
                         snapshot_root=str(tmp_path))
    target = tmp_path / "cam1" / "2026-05-21" / "vehicle_0042"
    assert target.is_dir()


def test_flush_writes_hero_and_angles(tmp_path):
    b = _seeded_buffer(3)
    flush_buffer_to_disk(b, last_seen=1779394907.2, event_kind="drive_by",
                         vehicle_class="car",
                         snapshot_root=str(tmp_path))
    track_dir = tmp_path / "cam1" / "2026-05-21" / "vehicle_0042"
    # hero is the highest-confidence crop (index 2 here, conf 0.7)
    hero_bytes = (track_dir / "hero.jpg").read_bytes()
    assert hero_bytes == bytes([0xFF, 0xD8]) + b"crop_2"
    angles = sorted((track_dir).glob("angle_*.jpg"))
    assert len(angles) == 2


def test_flush_writes_metadata_json(tmp_path):
    b = _seeded_buffer(3)
    flush_buffer_to_disk(b, last_seen=1779394907.2, event_kind="idle",
                         vehicle_class="truck",
                         snapshot_root=str(tmp_path))
    meta = json.loads(
        (tmp_path / "cam1" / "2026-05-21" / "vehicle_0042" / "metadata.json")
        .read_text()
    )
    assert meta["track_id"] == "vehicle_0042"
    assert meta["camera_id"] == "cam1"
    assert meta["first_seen"] == 1779394901.5
    assert meta["last_seen"] == 1779394907.2
    assert meta["duration_seconds"] == pytest.approx(5.7, abs=0.01)
    assert meta["event_kind"] == "idle"
    assert meta["vehicle_class"] == "truck"
    assert meta["hero_frame_index"] == 2
    assert meta["voting_samples"] == 3
    # Phase 1: attributes block exists with all-null values (no classifier)
    assert meta["attributes"]["color"] is None
    assert meta["attributes"]["body_type"] is None
    assert meta["attributes"]["make"] is None


def test_flush_empty_buffer_is_a_noop(tmp_path):
    b = TrackBuffer(track_id="vehicle_0099", camera_id="cam1",
                    first_seen=1779394901.5)
    flush_buffer_to_disk(b, last_seen=1779394907.2, event_kind="drive_by",
                         vehicle_class="car",
                         snapshot_root=str(tmp_path))
    # No directory created, no error raised
    assert not (tmp_path / "cam1").exists()
```

- [ ] **Step 7.2: Run, confirm import error**

Run: `pytest tests/test_vehicle_attributes_storage.py -v`
Expected: collection error.

- [ ] **Step 7.3: Implement storage.py**

Write to `services/vehicle-attributes/storage.py`:

```python
"""Filesystem layout writer for vehicle-attributes Phase 1.

Writes per-track directories at:
    /data/snapshots/vehicles/{camera}/{date}/{track_id}/
        hero.jpg            ← highest-confidence crop
        angle_NN.jpg        ← remaining crops (zero-padded index)
        metadata.json       ← track metadata + (Phase 1) null attribute block

Phase 1's metadata.json attribute block is all-null placeholders. Phase 3
will fill in {color, body_type, make} with classifier output. The all-null
shape is committed now so Phase 3 only adds values, doesn't restructure.
"""
import json
import logging
import os
from datetime import datetime

from buffer import TrackBuffer

logger = logging.getLogger("vehicle-attributes.storage")


def _date_str_from_first_seen(first_seen: float) -> str:
    """YYYY-MM-DD in local time (container TZ from LOCATION_TIMEZONE)."""
    return datetime.fromtimestamp(first_seen).strftime("%Y-%m-%d")


def flush_buffer_to_disk(
    buf: TrackBuffer,
    last_seen: float,
    event_kind: str,           # "drive_by" | "idle"
    vehicle_class: str,        # "car" | "truck" | "bus" | …
    snapshot_root: str,
) -> None:
    """Write the buffer to /data/snapshots/vehicles/{cam}/{date}/{track_id}/.

    Empty buffer = silent no-op.
    """
    if not buf.crops:
        logger.debug(
            f"Flush {buf.track_id}: empty buffer, skipping"
        )
        return

    date_str = _date_str_from_first_seen(buf.first_seen)
    track_dir = os.path.join(snapshot_root, buf.camera_id, date_str,
                             buf.track_id)
    os.makedirs(track_dir, exist_ok=True)

    hero_idx = buf.hero_index()

    # Hero
    hero_path = os.path.join(track_dir, "hero.jpg")
    with open(hero_path, "wb") as fh:
        fh.write(buf.crops[hero_idx])

    # Angles: every non-hero crop, zero-padded sequential
    angle_n = 1
    for i, crop in enumerate(buf.crops):
        if i == hero_idx:
            continue
        angle_path = os.path.join(track_dir, f"angle_{angle_n:02d}.jpg")
        with open(angle_path, "wb") as fh:
            fh.write(crop)
        angle_n += 1

    # Metadata
    meta = {
        "track_id": buf.track_id,
        "camera_id": buf.camera_id,
        "first_seen": buf.first_seen,
        "last_seen": last_seen,
        "duration_seconds": round(last_seen - buf.first_seen, 2),
        "event_kind": event_kind,
        "vehicle_class": vehicle_class,
        "hero_frame_index": hero_idx,
        "voting_samples": len(buf.crops),
        # Phase 1: classifier hasn't shipped yet. Shape committed; Phase 3
        # will populate non-null values. See spec §2.5.
        "attributes": {
            "color": None,
            "color_confidence": None,
            "body_type": None,
            "body_type_confidence": None,
            "make": None,
            "make_confidence": None,
            "model": None,
        },
        "snapshot_bbox": buf.bboxes[hero_idx],
    }
    meta_path = os.path.join(track_dir, "metadata.json")
    with open(meta_path, "w") as fh:
        json.dump(meta, fh, indent=2)

    logger.info(
        f"Flushed {buf.track_id} → {track_dir} "
        f"({len(buf.crops)} crops, hero=angle_{hero_idx}, kind={event_kind})"
    )
```

Write the shim at `services/vehicle_attributes/storage.py`:

```python
"""Re-export shim — actual source lives in ../vehicle-attributes/storage.py."""
from storage import *  # noqa: F401,F403
```

- [ ] **Step 7.4: Run storage tests, confirm 4 PASS**

Run: `pytest tests/test_vehicle_attributes_storage.py -v`
Expected: 4 PASS.

- [ ] **Step 7.5: Commit**

```bash
git add services/vehicle-attributes/storage.py services/vehicle_attributes/storage.py tests/test_vehicle_attributes_storage.py
git commit -m "vehicle-attributes: storage.py writes per-track dirs + metadata.json"
```

---

## Task 8: Service main loop (TDD)

**Files:**
- Create: `services/vehicle-attributes/service.py`
- Create: `services/vehicle_attributes/service.py` (shim)
- Test: `tests/test_vehicle_attributes_service.py`

- [ ] **Step 8.1: Write the failing tests**

Write to `tests/test_vehicle_attributes_service.py`:

```python
"""Service-level tests for vehicle-attributes Phase 1."""
import json
import pytest
from services.vehicle_attributes.service import (
    handle_event,
    _scale_bbox_sub_to_hd,
    _pad_bbox,
)


def test_scale_bbox_sub_to_hd_basic():
    # Sub-stream 896×512 → HD 2304×1296 (scales 2.571× / 2.531×)
    bbox_sub = [100, 50, 200, 150]
    hd = _scale_bbox_sub_to_hd(bbox_sub,
                               sub_size=(896, 512),
                               hd_size=(2304, 1296))
    assert hd[0] == int(100 * 2304 / 896)
    assert hd[2] == int(200 * 2304 / 896)
    assert hd[1] == int(50 * 1296 / 512)
    assert hd[3] == int(150 * 1296 / 512)


def test_pad_bbox_applies_padding():
    bbox = [100, 100, 200, 200]
    padded = _pad_bbox(bbox, pct=0.20, frame_size=(2304, 1296))
    assert padded[0] == 80
    assert padded[1] == 80
    assert padded[2] == 220
    assert padded[3] == 220


def test_pad_bbox_clips_at_frame_edge():
    bbox = [0, 0, 50, 50]
    padded = _pad_bbox(bbox, pct=1.0, frame_size=(2304, 1296))
    assert padded[0] == 0
    assert padded[1] == 0
    assert padded[2] == 100
    assert padded[3] == 100


def test_handle_vehicle_detected_opens_buffer():
    buffers = {}
    event = {
        "event_type": "vehicle_detected",
        "vehicle_id": "vehicle_0042",
        "vehicle_first_seen": "1779394901",
        "camera_id": "cam1",
        "bbox": json.dumps([10, 20, 50, 60]),
        "vehicle_confidence": "0.85",
        "vehicle_class": "car",
        "timestamp": "1779394901.5",
    }
    handle_event(event, buffers, r_bin=None, hd_size=(2304, 1296),
                 snapshot_root="/tmp")
    assert "vehicle_0042" in buffers
    assert buffers["vehicle_0042"].crops == []
    assert buffers["vehicle_0042"].first_seen == 1779394901.0


def test_handle_vehicle_left_flushes_and_deletes_buffer(tmp_path):
    from services.vehicle_attributes.buffer import TrackBuffer
    buf = TrackBuffer(track_id="vehicle_0042", camera_id="cam1",
                      first_seen=1779394901.5)
    buf.append(crop=b"\xff\xd8jpeg", yolo_conf=0.85, bbox=[10, 20, 50, 60])
    buffers = {"vehicle_0042": buf}

    event = {
        "event_type": "vehicle_left",
        "vehicle_id": "vehicle_0042",
        "camera_id": "cam1",
        "timestamp": "1779394907.2",
        "vehicle_class": "car",
    }
    handle_event(event, buffers, r_bin=None, hd_size=(2304, 1296),
                 snapshot_root=str(tmp_path))
    assert "vehicle_0042" not in buffers
    track_dir = tmp_path / "cam1" / "2026-05-21" / "vehicle_0042"
    assert (track_dir / "hero.jpg").is_file()
    assert (track_dir / "metadata.json").is_file()


def test_handle_unknown_event_type_is_ignored():
    buffers = {}
    event = {"event_type": "person_appeared", "person_id": "person_001"}
    handle_event(event, buffers, r_bin=None, hd_size=(2304, 1296),
                 snapshot_root="/tmp")
    assert buffers == {}


def test_handle_vehicle_idle_also_flushes(tmp_path):
    from services.vehicle_attributes.buffer import TrackBuffer
    buf = TrackBuffer(track_id="v_x", camera_id="cam1", first_seen=1779394901.5)
    buf.append(crop=b"data", yolo_conf=0.7, bbox=[1, 2, 3, 4])
    buffers = {"v_x": buf}

    event = {
        "event_type": "vehicle_idle",
        "vehicle_id": "v_x",
        "camera_id": "cam1",
        "timestamp": "1779394907.2",
        "vehicle_class": "car",
    }
    handle_event(event, buffers, r_bin=None, hd_size=(2304, 1296),
                 snapshot_root=str(tmp_path))
    assert "v_x" not in buffers
```

- [ ] **Step 8.2: Run, confirm import error**

Run: `pytest tests/test_vehicle_attributes_service.py -v`
Expected: collection error.

- [ ] **Step 8.3: Implement service.py**

Write to `services/vehicle-attributes/service.py`:

```python
"""Per-camera vehicle-attributes service — Phase 1 (capture + group only).

Subscribes to `events:{cam}`. On vehicle_detected opens a TrackBuffer. On
vehicle_sample pulls `frame_hd:{cam}` from Redis, crops, appends to buffer.
On vehicle_left OR vehicle_idle flushes the buffer to disk and removes it.

No classifier in this phase — Phase 3 will add a buffer→prediction step
before the disk flush.

Startup gate: reads cameras:registry to confirm both detect_vehicles AND
detect_vehicle_attributes are true; exits cleanly if either is false.
"""
import json
import logging
import os
import sys
import time

sys.path.insert(0, "/workspace")
import redis  # noqa: E402
import numpy as np  # noqa: E402
import cv2  # noqa: E402

from contracts.redis_client import make_redis_client  # noqa: E402
from contracts.streams import (  # noqa: E402
    EVENT_STREAM,
    HD_FRAME_KEY,
    REGISTRY_KEY,
)

from buffer import TrackBuffer  # noqa: E402
from storage import flush_buffer_to_disk  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("vehicle-attributes")

CAMERA_ID = os.getenv("CAMERA_ID", "")
SNAPSHOT_ROOT = os.getenv("SNAPSHOT_ROOT", "/data/snapshots/vehicles")
MAX_BUFFER_CROPS = int(os.getenv("MAX_BUFFER_CROPS", "8"))
MIN_CROP_AREA_HD_PX = int(os.getenv("MIN_CROP_AREA_HD_PX", "2500"))  # 50×50
CROP_PADDING_PCT = float(os.getenv("CROP_PADDING_PCT", "0.20"))
JPEG_QUALITY = int(os.getenv("JPEG_QUALITY", "85"))


# ---------------------------------------------------------------------------
# Geometry helpers (pure functions — unit-tested)
# ---------------------------------------------------------------------------

def _scale_bbox_sub_to_hd(bbox: list[int],
                          sub_size: tuple[int, int],
                          hd_size: tuple[int, int]) -> list[int]:
    sx = hd_size[0] / sub_size[0]
    sy = hd_size[1] / sub_size[1]
    return [int(bbox[0] * sx), int(bbox[1] * sy),
            int(bbox[2] * sx), int(bbox[3] * sy)]


def _pad_bbox(bbox: list[int], pct: float,
              frame_size: tuple[int, int]) -> list[int]:
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    dx = int(w * pct)
    dy = int(h * pct)
    return [
        max(0, bbox[0] - dx),
        max(0, bbox[1] - dy),
        min(frame_size[0], bbox[2] + dx),
        min(frame_size[1], bbox[3] + dy),
    ]


def _crop_hd_frame(hd_jpeg_bytes: bytes, bbox: list[int]) -> bytes | None:
    arr = np.frombuffer(hd_jpeg_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return None
    crop = img[bbox[1]:bbox[3], bbox[0]:bbox[2]]
    if crop.size == 0:
        return None
    ok, buf = cv2.imencode(".jpg", crop,
                           [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    if not ok:
        return None
    return buf.tobytes()


# ---------------------------------------------------------------------------
# Event handlers (tested in test_vehicle_attributes_service.py)
# ---------------------------------------------------------------------------

def handle_event(event: dict, buffers: dict[str, TrackBuffer],
                 r_bin,
                 hd_size: tuple[int, int],
                 snapshot_root: str) -> None:
    """Single-event dispatch. `r_bin` is None in unit tests that don't
    exercise the HD-frame fetch path; production passes the binary client."""
    et = event.get("event_type", "")
    if et == "vehicle_detected":
        _open_buffer(event, buffers)
    elif et == "vehicle_sample":
        _accumulate_crop(event, buffers, r_bin, hd_size)
    elif et in ("vehicle_left", "vehicle_idle"):
        _flush(event, buffers, snapshot_root)


def _open_buffer(event: dict, buffers: dict[str, TrackBuffer]) -> None:
    track_id = event.get("vehicle_id", "")
    if not track_id or track_id in buffers:
        return
    first_seen = float(event.get("vehicle_first_seen") or
                        event.get("timestamp", "0"))
    buffers[track_id] = TrackBuffer(
        track_id=track_id,
        camera_id=event.get("camera_id", ""),
        first_seen=first_seen,
        max_crops=MAX_BUFFER_CROPS,
    )
    logger.debug(f"opened buffer for {track_id}")


def _accumulate_crop(event: dict, buffers: dict[str, TrackBuffer],
                     r_bin, hd_size: tuple[int, int]) -> None:
    track_id = event.get("vehicle_id", "")
    buf = buffers.get(track_id)
    if buf is None:
        # Sample arrived before detected — open lazily.
        _open_buffer(event, buffers)
        buf = buffers[track_id]
    if buf.is_full() or r_bin is None:
        return

    cam = event.get("camera_id", "")
    hd_bytes = r_bin.get(HD_FRAME_KEY.format(camera_id=cam))
    if hd_bytes is None:
        logger.debug(f"HD frame missing for {cam} — skip sample {track_id}")
        return

    bbox_sub = json.loads(event.get("bbox", "[]"))
    if len(bbox_sub) != 4:
        return
    # Order: scale → pad → crop (spec §2.3)
    bbox_hd = _scale_bbox_sub_to_hd(bbox_sub,
                                    sub_size=(896, 512),
                                    hd_size=hd_size)
    bbox_padded = _pad_bbox(bbox_hd, CROP_PADDING_PCT, frame_size=hd_size)
    area = (bbox_padded[2] - bbox_padded[0]) * (bbox_padded[3] - bbox_padded[1])
    if area < MIN_CROP_AREA_HD_PX:
        return

    crop = _crop_hd_frame(hd_bytes, bbox_padded)
    if crop is None:
        return
    yolo_conf = float(event.get("vehicle_confidence", "0") or 0)
    buf.append(crop=crop, yolo_conf=yolo_conf, bbox=bbox_padded)
    buf.last_sampled_at = time.monotonic()


def _flush(event: dict, buffers: dict[str, TrackBuffer],
           snapshot_root: str) -> None:
    track_id = event.get("vehicle_id", "")
    buf = buffers.pop(track_id, None)
    if buf is None:
        return
    last_seen = float(event.get("timestamp", "0") or 0)
    event_kind = "idle" if event.get("event_type") == "vehicle_idle" else "drive_by"
    flush_buffer_to_disk(
        buf,
        last_seen=last_seen,
        event_kind=event_kind,
        vehicle_class=event.get("vehicle_class", ""),
        snapshot_root=snapshot_root,
    )


# ---------------------------------------------------------------------------
# Startup gate + main loop (not unit-tested; covered by Task 15)
# ---------------------------------------------------------------------------

def _check_registry_wants_attributes(r) -> bool:
    """Read cameras:registry. Exit cleanly if either flag is false."""
    raw = r.hget(REGISTRY_KEY, CAMERA_ID)
    if not raw:
        logger.warning(f"camera {CAMERA_ID} not in registry — exiting")
        return False
    cam = json.loads(raw)
    if cam.get("detect_vehicles") is False:
        logger.info(f"{CAMERA_ID}: detect_vehicles=false — exiting cleanly")
        return False
    if cam.get("detect_vehicle_attributes") is not True:
        logger.info(
            f"{CAMERA_ID}: detect_vehicle_attributes=false — exiting cleanly"
        )
        return False
    return True


def main() -> int:
    if not CAMERA_ID:
        logger.error("CAMERA_ID env var not set — exiting")
        return 1

    r = make_redis_client()
    r_bin = make_redis_client(decode_responses=False)

    if not _check_registry_wants_attributes(r):
        return 0

    stream_key = EVENT_STREAM.format(camera_id=CAMERA_ID)
    logger.info(
        f"vehicle-attributes-{CAMERA_ID} started — subscribing to {stream_key}"
    )

    buffers: dict[str, TrackBuffer] = {}
    last_id = "$"
    hd_size = (
        int(os.getenv("HD_FRAME_WIDTH", "2304")),
        int(os.getenv("HD_FRAME_HEIGHT", "1296")),
    )

    while True:
        try:
            result = r.xread({stream_key: last_id}, block=2000, count=50)
        except redis.RedisError as e:
            logger.warning(f"xread error: {e}; retrying in 1s")
            time.sleep(1.0)
            continue

        if not result:
            continue

        for _stream, entries in result:
            for entry_id, fields in entries:
                last_id = entry_id
                try:
                    handle_event(fields, buffers, r_bin, hd_size,
                                 SNAPSHOT_ROOT)
                except Exception as e:
                    logger.exception(
                        f"handle_event failed for "
                        f"{fields.get('event_type')}: {e}"
                    )


if __name__ == "__main__":
    sys.exit(main())
```

Write the shim `services/vehicle_attributes/service.py`:

```python
"""Re-export shim — actual source lives in ../vehicle-attributes/service.py."""
from service import *  # noqa: F401,F403
```

- [ ] **Step 8.4: Run service tests, confirm 7 PASS**

Run: `pytest tests/test_vehicle_attributes_service.py -v`
Expected: 7 PASS.

- [ ] **Step 8.5: Confirm pytest + ruff clean across the project**

Run: `pytest -q && ruff check .`
Expected: All passing, ruff clean.

- [ ] **Step 8.6: Commit**

```bash
git add services/vehicle-attributes/service.py services/vehicle_attributes/service.py tests/test_vehicle_attributes_service.py
git commit -m "vehicle-attributes: service main loop + HD-frame crop pipeline"
```

---

## Task 9: docker-compose blocks for cam1–cam20

**Files:**
- Modify: `docker-compose.yml`

20 new blocks plus an env-var addition to every existing `tracker-camN`. Mechanical but large.

- [ ] **Step 9.1: Confirm the existing vehicle-detector-cam1 anchor line**

Run: `grep -n '^  vehicle-detector-cam1:\|^  vehicle-detector-cam20:' docker-compose.yml`
Expected: two line numbers (cam1 ~L219).

- [ ] **Step 9.2: Add the cam1 block**

Immediately after the `vehicle-detector-cam1:` block's closing `restart: unless-stopped` line, add:

```yaml
  vehicle-attributes-cam1:
    build:
      context: ./services/vehicle-attributes
    profiles: ["cam1"]
    environment:
      - CAMERA_ID=cam1
      - REDIS_HOST=redis
      - REDIS_PASSWORD=${REDIS_PASSWORD:-}
      - REDIS_PORT=6379
      - SNAPSHOT_ROOT=/data/snapshots/vehicles
      - SAMPLE_INTERVAL_FRAMES=${SAMPLE_INTERVAL_FRAMES:-3}
      - MAX_BUFFER_CROPS=${MAX_BUFFER_CROPS:-8}
      - MIN_CROP_AREA_HD_PX=${MIN_CROP_AREA_HD_PX:-2500}
      - CROP_PADDING_PCT=${CROP_PADDING_PCT:-0.20}
      - JPEG_QUALITY=${JPEG_QUALITY:-85}
      - HD_FRAME_WIDTH=${HD_FRAME_WIDTH:-2304}
      - HD_FRAME_HEIGHT=${HD_FRAME_HEIGHT:-1296}
      - TZ=${LOCATION_TIMEZONE:-America/Toronto}
    volumes:
      - ./contracts:/app/contracts:ro
      - ./:/workspace:ro
      - snapshot-data:/data/snapshots
    depends_on:
      redis:
        condition: service_healthy
      tracker-cam1:
        condition: service_started
    restart: unless-stopped
```

- [ ] **Step 9.3: Wire EMIT_VEHICLE_SAMPLES into tracker-cam1's environment block**

Find `tracker-cam1:` (~L147). Inside its `environment:` list, add:

```yaml
      - EMIT_VEHICLE_SAMPLES=${EMIT_VEHICLE_SAMPLES:-0}
      - SAMPLE_INTERVAL_FRAMES=${SAMPLE_INTERVAL_FRAMES:-3}
```

Default `0` keeps trackers running today emit-free. Operators flipping `detect_vehicle_attributes=true` set `EMIT_VEHICLE_SAMPLES=1` in `.env`. Phase 4 will wire this through the setup wizard.

- [ ] **Step 9.4: Replicate the additions across cam2–cam20**

For each of cam2…cam20, repeat 9.2 + 9.3 swapping `cam1 → camN` in:
- service name (`vehicle-attributes-camN`)
- `profiles:` list
- `CAMERA_ID=camN`
- `depends_on: tracker-camN`

After all 20 are added, run:

```bash
grep -c '^  vehicle-attributes-cam' docker-compose.yml
```

Expected output: `20`.

- [ ] **Step 9.5: Validate compose syntax**

Run: `docker compose config --services 2>&1 | grep vehicle-attributes | wc -l`
Expected: `20`.

- [ ] **Step 9.6: Commit**

```bash
git add docker-compose.yml
git commit -m "compose: add vehicle-attributes-camN service (cam1-cam20)"
```

---

## Task 10: UI — add-camera form

**Files:**
- Modify: `services/dashboard/static/cameras.html` (around line 154)
- Modify: `services/dashboard/static/js/pages/cameras.js` (around line 361-363)

- [ ] **Step 10.1: Add the new checkbox to the add-camera form**

In `services/dashboard/static/cameras.html`, locate the three existing checkboxes (~L147-155). Add a fourth label after `camDetectFaces`:

```html
                            <label style="display:flex;align-items:center;gap:0.4rem;cursor:pointer;">
                                <input type="checkbox" id="camDetectVehicleAttributes" data-requires="camDetectVehicles"> Vehicle attributes (requires vehicle detection)
                            </label>
```

- [ ] **Step 10.2: Bump cameras.js cache-bust to v=9**

In `cameras.html`:

```html
    <script src="js/pages/cameras.js?v=9"></script>
```

- [ ] **Step 10.3: Wire the new checkbox into the POST body**

In `services/dashboard/static/js/pages/cameras.js` `handleAddCamera`, after the three existing `body.detect_*` assignments, add:

```javascript
        body.detect_vehicle_attributes = $('camDetectVehicleAttributes').checked;
```

- [ ] **Step 10.4: Manual smoke**

`docker compose restart dashboard`. Hard-refresh `/cameras.html`. Confirm:
- The new checkbox appears beside the existing three
- Unchecking "Vehicles" auto-disables + unchecks "Vehicle attributes"
- Re-checking "Vehicles" re-enables "Vehicle attributes" (left unchecked per established `checkbox-dependencies.js` behavior)

- [ ] **Step 10.5: Commit**

```bash
git add services/dashboard/static/cameras.html services/dashboard/static/js/pages/cameras.js
git commit -m "ui(cameras): add detect_vehicle_attributes checkbox to add-camera form"
```

---

## Task 11: UI — edit-camera modal

**Files:**
- Modify: `services/dashboard/static/cameras.html` (the modal block added in PR #20)
- Modify: `services/dashboard/static/js/pages/cameras.js` (`openEditModal`, `handleSaveEdit`)

- [ ] **Step 11.1: Add checkbox to the modal**

Locate the edit modal's detector checkboxes (added in PR #20). Add a fourth below the `editDetectFaces` block:

```html
                            <label style="display:flex;align-items:center;gap:0.4rem;cursor:pointer;">
                                <input type="checkbox" id="editDetectVehicleAttributes" data-requires="editDetectVehicles"> Vehicle attributes (requires vehicle detection)
                            </label>
```

- [ ] **Step 11.2: Populate the new field when opening the modal**

In `cameras.js` `openEditModal`, after `$('editDetectFaces').checked = ...`, add:

```javascript
        $('editDetectVehicleAttributes').checked = cam.detect_vehicle_attributes === true;
```

Then dispatch a change event on `editDetectVehicles` so the dep-syncer runs:

```javascript
        $('editDetectVehicles').dispatchEvent(new Event('change'));
```

- [ ] **Step 11.3: Send the field on PUT**

In `handleSaveEdit`, after the three `const detect_*` lines, add:

```javascript
    const detect_vehicle_attributes = $('editDetectVehicleAttributes').checked;
```

Then in the `body` object:

```javascript
        detect_vehicle_attributes,
```

- [ ] **Step 11.4: Bump cameras.js cache-bust to v=10**

In `cameras.html`:

```html
    <script src="js/pages/cameras.js?v=10"></script>
```

- [ ] **Step 11.5: Manual smoke**

Hard-refresh `/cameras.html`. Click ✏. Confirm:
- New checkbox appears
- Toggling "Vehicles" off auto-disables + unchecks "Vehicle attributes"
- Saving with the new flag → `docker compose ps` shows `vehicle-attributes-cam{N}` come up within ~6 s (after Task 9 compose blocks shipped)

- [ ] **Step 11.6: Commit**

```bash
git add services/dashboard/static/cameras.html services/dashboard/static/js/pages/cameras.js
git commit -m "ui(cameras): add detect_vehicle_attributes to edit modal"
```

---

## Task 12: UI — setup wizard Step 4

**Files:**
- Modify: `services/dashboard/static/setup.html` (around line 177)
- Modify: `services/dashboard/static/js/pages/setup.js`

- [ ] **Step 12.1: Add checkbox to the setup wizard**

In `setup.html`, locate the existing detector checkboxes (PR #19 wired `data-requires` here around L175-180). Add below `detectFaces`:

```html
                        <label style="display:flex;align-items:center;gap:0.4rem;cursor:pointer;">
                            <input type="checkbox" id="detectVehicleAttributes" data-requires="detectVehicles"> Vehicle attributes (requires vehicle detection)
                        </label>
```

- [ ] **Step 12.2: Wire into setup.js camera-creation payload**

In `setup.js`, find where the first-camera POST body is built (search for `detect_faces:`). Add directly after:

```javascript
            detect_vehicle_attributes: document.getElementById('detectVehicleAttributes')?.checked || false,
```

Bump any cache-bust `?v=` on setup.html's setup.js script tag.

- [ ] **Step 12.3: Commit**

```bash
git add services/dashboard/static/setup.html services/dashboard/static/js/pages/setup.js
git commit -m "ui(setup): add detect_vehicle_attributes to first-camera step"
```

---

## Task 13: Browse — grouped-cards backend

**Files:**
- Modify: `services/dashboard/routes/browse.py`

Add a new endpoint that walks per-track dirs (Phase 1 layout); keep the existing flat-snapshot endpoint for backwards compat (dual-format support per spec §6 Q6).

- [ ] **Step 13.1: Add the new endpoint**

In `services/dashboard/routes/browse.py`, after `list_day_snapshots` (~L110), add:

```python
@router.get("/tracks/{date}")
async def list_day_tracks(date: str, camera: str = ""):
    """List per-track snapshot groups for a day (Phase 1 vehicle-attributes
    layout). Each entry is one track_id directory with hero + angle thumbs.

    Returns [{
        track_id, camera, date, time, hero_url, angle_urls: [...],
        vehicle_class, event_kind, duration_seconds, voting_samples,
        attributes  // null fields in Phase 1, populated in Phase 3
    }] sorted newest-first.
    """
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return JSONResponse(status_code=400, content={"error": "invalid date"})

    tracks = []
    cams_to_scan = [camera] if camera else _list_cam_subdirs()
    for cam in cams_to_scan:
        cam_safe = re.sub(r"[^a-zA-Z0-9_-]", "", cam)
        day_dir = os.path.join(SNAPSHOT_ROOT, cam_safe, date)
        if not os.path.isdir(day_dir):
            continue
        for entry in os.scandir(day_dir):
            if not entry.is_dir():
                continue
            meta_path = os.path.join(entry.path, "metadata.json")
            if not os.path.isfile(meta_path):
                continue
            try:
                with open(meta_path) as fh:
                    meta = json.load(fh)
            except (OSError, ValueError):
                continue
            track_id = meta.get("track_id", entry.name)
            angles = sorted(
                f.name for f in os.scandir(entry.path)
                if f.is_file() and f.name.startswith("angle_")
                and f.name.endswith(".jpg")
            )
            tracks.append({
                "track_id": track_id,
                "camera": cam_safe,
                "date": date,
                "time": datetime.fromtimestamp(meta.get("first_seen", 0)).strftime("%H:%M:%S"),
                "first_seen": meta.get("first_seen"),
                "hero_url": f"/api/browse/tracks/{date}/{cam_safe}/{track_id}/hero.jpg",
                "angle_urls": [
                    f"/api/browse/tracks/{date}/{cam_safe}/{track_id}/{a}"
                    for a in angles
                ],
                "vehicle_class": meta.get("vehicle_class", "vehicle"),
                "event_kind": meta.get("event_kind", ""),
                "duration_seconds": meta.get("duration_seconds", 0),
                "voting_samples": meta.get("voting_samples", 1),
                "attributes": meta.get("attributes", {}),
            })

    tracks.sort(key=lambda t: t.get("first_seen") or 0, reverse=True)
    return tracks


@router.get("/tracks/{date}/{camera}/{track_id}/{filename}",
            name="serve_track_image")
async def serve_track_image(date: str, camera: str, track_id: str,
                            filename: str):
    """Serve hero.jpg or angle_NN.jpg from a per-track directory.

    Defense: all four path components match strict character classes AND
    the resolved real path must stay inside SNAPSHOT_ROOT (same containment
    check pattern used by `routes/events.py:get_event_snapshot`).
    """
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return JSONResponse(status_code=400, content={"error": "invalid date"})
    if not re.match(r"^[a-zA-Z0-9_-]+$", camera):
        return JSONResponse(status_code=400, content={"error": "invalid camera"})
    if not re.match(r"^[a-zA-Z0-9_-]+$", track_id):
        return JSONResponse(status_code=400, content={"error": "invalid track_id"})
    if not re.match(r"^(hero\.jpg|angle_\d{2}\.jpg)$", filename):
        return JSONResponse(status_code=400, content={"error": "invalid filename"})

    candidate = os.path.realpath(os.path.join(SNAPSHOT_ROOT, camera, date,
                                              track_id, filename))
    root_real = os.path.realpath(SNAPSHOT_ROOT)
    if not candidate.startswith(root_real + os.sep):
        return JSONResponse(status_code=400, content={"error": "out of range"})
    if not os.path.isfile(candidate):
        return JSONResponse(status_code=404, content={"error": "not found"})
    return FileResponse(candidate, media_type="image/jpeg")
```

Add at top of file if missing (most should already be imported):

```python
import re
from datetime import datetime
```

- [ ] **Step 13.2: Smoke test the endpoint**

After dashboard restart:

```bash
curl -s http://localhost:8080/api/browse/tracks/2026-05-21 | jq .
```

Expected: `[]` (no per-track dirs exist yet). Status 200.

- [ ] **Step 13.3: Commit**

```bash
git add services/dashboard/routes/browse.py
git commit -m "browse: add /api/browse/tracks/{date} for per-track grouped cards"
```

---

## Task 14: Browse — grouped-cards frontend

**Files:**
- Modify: `services/dashboard/static/js/dashboard/browse.js`
- Modify: `services/dashboard/static/css/style.css`

- [ ] **Step 14.1: Confirm current browse.js shape**

Run: `grep -n 'function\|_browseDay\|tracks\|days' services/dashboard/static/js/dashboard/browse.js | head -20`

Existing code uses event delegation via `data-action=` (post-PR #20). The new track-cards renderer follows the same pattern.

- [ ] **Step 14.2: Add a tracks renderer function**

In `browse.js`, add a function that fetches `/api/browse/tracks/{date}` and renders grouped cards using the established DOMPurify-sanitized assignment pattern (`_safeHtml()` is already imported in this file from PR #20's safe-html.js extract):

```javascript
async function _renderDayTracks(date, camera) {
    const url = `/api/browse/tracks/${encodeURIComponent(date)}`
                + (camera ? `?camera=${encodeURIComponent(camera)}` : '');
    const res = await fetch(url);
    if (!res.ok) return '';
    const tracks = await res.json();
    if (!Array.isArray(tracks) || tracks.length === 0) return '';

    const html = tracks.map(t => `
        <div class="track-card" data-track-id="${escape(t.track_id)}">
            <div class="track-hero">
                <img src="${t.hero_url}" alt="${escape(t.vehicle_class)} hero"
                     loading="lazy">
                <span class="track-class-pill">${escape(t.vehicle_class)}</span>
            </div>
            <div class="track-meta">
                <div class="track-time">${escape(t.time)}
                    <span class="track-event">${escape(t.event_kind)}</span>
                </div>
                <div class="track-stats">
                    ${t.voting_samples} angle${t.voting_samples === 1 ? '' : 's'}
                    · ${t.duration_seconds.toFixed(1)}s
                </div>
            </div>
            <div class="track-angles">
                ${t.angle_urls.map(u => `
                    <img src="${u}" class="track-angle" loading="lazy" alt="angle">
                `).join('')}
            </div>
        </div>
    `).join('');

    return `<section class="track-cards-section">
        <h3 style="margin:0.75rem 0 0.5rem;font-size:0.9rem;color:#94a3b8;">
            Per-track view (${tracks.length}
            track${tracks.length === 1 ? '' : 's'})
        </h3>
        <div class="track-cards-grid">${html}</div>
    </section>`;
}
```

In the existing per-day render path (the function that currently fetches `/api/browse/days/{date}` and writes the result to the browse container via `_safeHtml()`), prepend the tracks-section HTML returned by `_renderDayTracks(...)` to the existing flat-grid HTML string BEFORE the `_safeHtml(...)` call that hands the combined string to the container. The combined string still flows through `_safeHtml` so DOMPurify sanitization is preserved.

- [ ] **Step 14.3: Add CSS for the cards**

Append to `services/dashboard/static/css/style.css`:

```css
/* Phase 1 vehicle-attributes — per-track grouped cards (browse tab) */
.track-cards-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 0.75rem;
}
.track-card {
    background: #1a2235;
    border: 1px solid #2d3748;
    border-radius: 10px;
    overflow: hidden;
    display: flex;
    flex-direction: column;
}
.track-hero {
    position: relative;
    aspect-ratio: 16/9;
    background: #0f172a;
}
.track-hero img { width: 100%; height: 100%; object-fit: cover; }
.track-class-pill {
    position: absolute; top: 6px; left: 6px;
    background: rgba(15,23,42,0.85); color: #4ade80;
    font-size: 0.7rem; padding: 2px 8px; border-radius: 10px;
}
.track-meta {
    padding: 0.5rem 0.75rem;
    display: flex; justify-content: space-between; align-items: center;
    font-size: 0.8rem; color: #cbd5e1;
}
.track-event { color: #94a3b8; margin-left: 0.4rem; font-style: italic; }
.track-stats { color: #64748b; font-size: 0.75rem; }
.track-angles {
    display: flex; gap: 4px; padding: 0 0.75rem 0.75rem;
    overflow-x: auto;
}
.track-angle {
    height: 60px; width: 80px; object-fit: cover; border-radius: 4px;
    flex-shrink: 0;
}
```

Bump the cache-bust on every HTML file that loads style.css (run `grep -l 'style.css' services/dashboard/static/*.html`): change `?v=5` → `?v=6`.

- [ ] **Step 14.4: Bump browse.js cache-bust**

In each HTML file that loads `js/dashboard/browse.js`, bump the `?v=N` query string by one.

- [ ] **Step 14.5: Manual smoke**

`docker compose restart dashboard`. Hard-refresh `/`. Open the browse panel on a date that has per-track data. Confirm the per-track grouped cards render above the flat-snapshot grid; flat grid still renders for backwards compat. (At this point per-track data won't exist yet — Task 15 covers end-to-end.)

- [ ] **Step 14.6: Commit**

```bash
git add services/dashboard/static/js/dashboard/browse.js services/dashboard/static/css/style.css services/dashboard/static/index.html services/dashboard/static/single.html
git commit -m "ui(browse): render per-track grouped cards above flat grid"
```

---

## Task 15: End-to-end verification

**Files:** none (verification only)

- [ ] **Step 15.1: Build the new service image + bring it up on cam1**

```bash
docker compose build vehicle-attributes-cam1
```

Expected: image builds in ~30 s (no GPU layers, slim base).

- [ ] **Step 15.2: Enable the flag on cam1 via the edit modal**

In the dashboard: Cameras tab → click ✏ on cam1 → tick "Vehicle attributes" → Save.

Expected within ~6 s: `docker compose ps` shows `vision-labs-vehicle-attributes-cam1-1` up.

- [ ] **Step 15.3: Set EMIT_VEHICLE_SAMPLES=1 in .env + recreate tracker-cam1**

```bash
grep -q '^EMIT_VEHICLE_SAMPLES=' .env || echo 'EMIT_VEHICLE_SAMPLES=1' >> .env
docker compose up -d --force-recreate --no-deps tracker-cam1
```

(Phase 4 will wire this into the setup wizard; Phase 1 is manual.)

- [ ] **Step 15.4: Watch the events stream for `vehicle_sample` entries**

```bash
REDIS_PW=$(grep '^REDIS_PASSWORD=' .env | cut -d= -f2)
docker exec vision-labs-redis-1 redis-cli -a "$REDIS_PW" --no-auth-warning \
    XREVRANGE events:cam1 + - COUNT 20 \
  | grep -A1 vehicle_sample | head -20
```

Expected: matched-vehicle updates produce `vehicle_sample` entries with the right `vehicle_id`. Drive a car past the camera (or wait for ambient traffic) for a few minutes.

- [ ] **Step 15.5: Confirm per-track directories appear after a drive-by**

```bash
ls /data/snapshots/vehicles/cam1/$(date +%Y-%m-%d)/
```

Expected: directories like `vehicle_0042/` with `hero.jpg`, `angle_01.jpg`, …, `metadata.json`.

- [ ] **Step 15.6: Verify metadata.json shape**

```bash
cat /data/snapshots/vehicles/cam1/$(date +%Y-%m-%d)/vehicle_*/metadata.json | head -30
```

Expected: track_id + vehicle_class + event_kind ("drive_by" or "idle") + voting_samples ≥ 1 + all-null `attributes` block.

- [ ] **Step 15.7: Hard-refresh `/single.html?cam=cam1` → Browse panel**

Expected: per-track grouped cards section appears above flat snapshots, one card per track with hero + angle thumbnails.

- [ ] **Step 15.8: Test the back-out path**

Edit modal → untick "Vehicle attributes" → Save. Within ~6 s, `vehicle-attributes-cam1` goes from Up → Exited (0). Existing per-track dirs stay on disk.

---

## Task 16: Docs

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `CONTEXT.md`

- [ ] **Step 16.1: Add CHANGELOG entry**

In `CHANGELOG.md` under `## [Unreleased]` → `### Added`, append:

```markdown
- **Vehicle attributes Phase 1** — new per-cam `vehicle-attributes-cam{N}` service buffers HD crops per tracked vehicle and writes `/data/snapshots/vehicles/{cam}/{date}/{track_id}/` containing `hero.jpg`, `angle_NN.jpg`, `metadata.json`. No classifier yet — Phase 3 ships ML. New `detect_vehicle_attributes` flag (hard-depends on `detect_vehicles`); tracker emits `vehicle_sample` every Nth matched update (gated by `EMIT_VEHICLE_SAMPLES`, off by default). Orchestrator allowlist + per-cam expansion extended. Browse renders grouped cards above the flat snapshot grid. *Requires building the new service image + tracker rebuild for the sample-emit code.*
```

- [ ] **Step 16.2: Add CONTEXT.md service inventory entry**

In `CONTEXT.md` §4, after §4.5 face-recognizer, add a new subsection (renumber subsequent sections):

```markdown
### 4.6 `vehicle-attributes/` (per-cam, no GPU in Phase 1)
- **Per-track HD-crop buffer.** Consumes `events:{cam}` filtered to `vehicle_detected` / `vehicle_sample` / `vehicle_left` / `vehicle_idle`. Maintains in-memory `dict[track_id, TrackBuffer]`; cap 8 crops per track.
- **HD frame source:** `GET frame_hd:{cam}` (binary Redis client, 5 s TTL set by camera-ingester). On miss, skip the sample — next event tries again.
- **Cropping pipeline:** sub→HD bbox scale (×2.571 / ×2.531) → 20% padding → `cv2.imdecode` → slice → `cv2.imencode` JPEG q=85.
- **Storage:** flushes to `/data/snapshots/vehicles/{cam}/{date}/{track_id}/{hero.jpg,angle_NN.jpg,metadata.json}` on `vehicle_left` or `vehicle_idle`. Hero = highest-confidence crop. Empty buffer = silent no-op.
- **Registry gate:** exits cleanly at startup if `detect_vehicles=false` OR `detect_vehicle_attributes!=true` (mirrors face-recognizer's pattern).
- **No GPU in Phase 1** — Dockerfile uses `python:3.11-slim`, no PyTorch/ONNX. Phase 3 will add the multi-head classifier per the spec at `docs/superpowers/specs/2026-05-21-vehicle-attribute-classification-design.md`. All-null `attributes` block in metadata.json is committed now so Phase 3 only fills values, doesn't restructure.
- **Producer/consumer:** consumes `events:{cam}`, `frame_hd:{cam}`, `cameras:registry`. Writes `/data/snapshots/vehicles/{cam}/...` (filesystem).
```

Also bump CLAUDE.md or CONTEXT.md §3's "6 services per slot" claim to 7 — the per-cam service count just grew.

- [ ] **Step 16.3: Run full pytest + ruff one more time**

```bash
pytest -q && ruff check .
```

Expected: All passing.

- [ ] **Step 16.4: Commit docs**

```bash
git add CHANGELOG.md CONTEXT.md
git commit -m "docs: CHANGELOG + CONTEXT entries for vehicle-attributes phase 1"
```

---

## Task 17: PR

**Files:** none (git ops only)

- [ ] **Step 17.1: Push the branch**

```bash
git push -u origin feat/vehicle-attributes-phase-1
```

- [ ] **Step 17.2: Open the PR**

```bash
gh pr create --title "feat(vehicle-attributes): Phase 1 — per-track HD crops + grouped browse" --body "$(cat <<'EOF'
## Summary
- New per-cam service `vehicle-attributes-cam{N}` — buffers HD crops per tracked vehicle, writes `/data/snapshots/vehicles/{cam}/{date}/{track_id}/{hero.jpg,angle_NN.jpg,metadata.json}` on `vehicle_left`/`vehicle_idle`. **No classifier yet** (Phase 3 ships ML).
- Tracker emits `vehicle_sample` events every 3rd matched update (gated by `EMIT_VEHICLE_SAMPLES`, off by default).
- `detect_vehicle_attributes` registry flag — hard-depends on `detect_vehicles`, plumbed through setup wizard + add-camera + edit-camera modal.
- Orchestrator allowlist + per-cam expansion extended for the new `vehicle-attributes` prefix.
- Browse renders per-track grouped cards above the flat snapshot grid.

## Test plan
- [x] pytest — adds ~22 new tests (buffer, storage, service handlers, tracker emission, validation, orchestrator allowlist)
- [x] ruff — clean
- [ ] Reviewer: `docker compose build vehicle-attributes-cam1 && docker compose up -d --force-recreate tracker-cam1`
- [ ] Reviewer: enable `detect_vehicle_attributes` on cam1 via ✏ edit modal → confirm `vehicle-attributes-cam1` spawns within ~6 s
- [ ] Reviewer: drive a car past cam1, watch `events:cam1` for `vehicle_sample` entries, confirm per-track dirs appear at `/data/snapshots/vehicles/cam1/$(date +%F)/`
- [ ] Reviewer: open Browse panel → confirm grouped cards render with hero + angle thumbnails

## Spec
`docs/superpowers/specs/2026-05-21-vehicle-attribute-classification-design.md` §1.3, §2.1–2.5, §5 Phase 1.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-review notes (already addressed)

- **Spec §9 deployment surfaces:** §9.1 setup wizard → Task 12. §9.2 add-camera → Task 10. §9.3 generic UI dep enforcement → already shipped in PR #19. §9.4 server validation → Task 3. §9.5 server defaults (`upsert_camera`) → Task 3. §9.6 slot estimator + §9.7 tier interaction + §9.8 GPU assignment → **deferred to Phase 3** because they only matter once the classifier ships and per-cam VRAM cost is non-zero. Phase 1 is CPU-only, slot count is unaffected.
- **Spec §6 open questions:** All decided in-spec.
- **Spec §7 Phase 1 acceptance:** Tracker emits `vehicle_sample` (Task 2) ✓, attribute service populates per-track dirs (Tasks 5-9, 15) ✓, Browse renders grouped cards (Tasks 13-14) ✓, no regression in existing event feed (Tasks 2.7 + 15.8) ✓.
- **Type consistency:** `TrackBuffer(track_id, camera_id, first_seen, …)` constructor signature used identically in buffer.py, storage.py, service.py, and all three test files. `flush_buffer_to_disk(buf, last_seen, event_kind, vehicle_class, snapshot_root)` signature consistent everywhere.
- **No placeholders.**

---

## Execution handoff

**Plan complete and saved to `docs/superpowers/plans/2026-05-21-vehicle-attributes-phase-1.md`.** Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, two-stage review (spec compliance + code quality) between tasks, fast iteration.

**2. Inline Execution** — I execute tasks in this session with batch checkpoints.

**Which approach?**
