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


def test_vehicle_detected_drops_stale_buffer_from_previous_tracker_session():
    """Live regression — at 17:14 prev tracker session opened a buffer
    for vehicle_0003 (first_seen=17:14:06). The tracker restarted at
    17:24 mid-track (vehicle_gone never fired). At 17:28 the new tracker
    issued vehicle_0003 to a different physical vehicle (first_seen=
    17:28:10). Without this fix, vehicle_detected was a no-op because
    the buffer key already existed, and the new vehicle's samples got
    appended to the orphan buffer — the eventual flush wrote to the
    OLD dir (first_seen=17:14:06) overwriting it, with metadata showing
    duration=887s and the prev vehicle's classifier vote."""
    from services.vehicle_attributes.buffer import TrackBuffer
    stale = TrackBuffer(track_id="vehicle_0003", camera_id="cam1",
                        first_seen=1779470046.0)
    stale.append(crop=b"\xff\xd8old", yolo_conf=0.5,
                 bbox=[735, 323, 809, 358])
    buffers = {"vehicle_0003": stale}

    fresh_event = {
        "event_type": "vehicle_detected",
        "vehicle_id": "vehicle_0003",
        "vehicle_first_seen": "1779470890",  # new tracker session
        "camera_id": "cam1",
        "bbox": json.dumps([453, 370, 552, 407]),
        "vehicle_confidence": "0.6",
        "vehicle_class": "truck",
        "timestamp": "1779470890.4",
    }
    handle_event(fresh_event, buffers, r_bin=None, hd_size=(2304, 1296),
                 snapshot_root="/tmp")

    buf = buffers["vehicle_0003"]
    assert buf.first_seen == 1779470890.0, \
        f"buffer must adopt new first_seen, got {buf.first_seen}"
    assert buf.crops == [], "stale crops must be dropped"


def test_vehicle_detected_keeps_buffer_when_first_seen_matches():
    """Idempotent vehicle_detected (same track_id + same first_seen) must
    not wipe the buffer's accumulated crops."""
    from services.vehicle_attributes.buffer import TrackBuffer
    buf = TrackBuffer(track_id="vehicle_0003", camera_id="cam1",
                      first_seen=1779394901.0)
    buf.append(crop=b"\xff\xd8keep", yolo_conf=0.7,
               bbox=[100, 100, 200, 200])
    buffers = {"vehicle_0003": buf}

    duplicate_event = {
        "event_type": "vehicle_detected",
        "vehicle_id": "vehicle_0003",
        "vehicle_first_seen": "1779394901",
        "camera_id": "cam1",
        "bbox": json.dumps([100, 100, 200, 200]),
        "vehicle_confidence": "0.7",
        "vehicle_class": "car",
        "timestamp": "1779394901.5",
    }
    handle_event(duplicate_event, buffers, r_bin=None, hd_size=(2304, 1296),
                 snapshot_root="/tmp")

    assert len(buffers["vehicle_0003"].crops) == 1, \
        "idempotent vehicle_detected must not wipe an existing buffer"


def test_vehicle_sample_drops_stale_buffer_via_lazy_open():
    """If the tracker restarts mid-track and the new tracker session's
    very first event we receive is a vehicle_sample (not vehicle_detected
    — possible if vehicle_detected raced ahead via a different consumer),
    the lazy-open path in _accumulate_crop must also detect the stale
    buffer and drop it. Otherwise samples accumulate into the orphan."""
    from services.vehicle_attributes.buffer import TrackBuffer
    stale = TrackBuffer(track_id="vehicle_0003", camera_id="cam1",
                        first_seen=1779470046.0)
    stale.append(crop=b"\xff\xd8old", yolo_conf=0.5,
                 bbox=[100, 100, 200, 200])
    buffers = {"vehicle_0003": stale}

    sample_event = {
        "event_type": "vehicle_sample",
        "vehicle_id": "vehicle_0003",
        "vehicle_first_seen": "1779470890",
        "camera_id": "cam1",
        "bbox": json.dumps([453, 370, 552, 407]),
        "vehicle_confidence": "0.6",
        "vehicle_class": "truck",
        "timestamp": "1779470890.4",
        "hd_snapshot_key": "",
    }
    # r_bin=None means _accumulate_crop early-returns before HD fetch,
    # but the buffer-staleness check runs first.
    handle_event(sample_event, buffers, r_bin=None, hd_size=(2304, 1296),
                 snapshot_root="/tmp")

    buf = buffers["vehicle_0003"]
    assert buf.first_seen == 1779470890.0, \
        f"lazy-open path must adopt new first_seen, got {buf.first_seen}"
    assert buf.crops == [], "stale crops must be dropped"


def test_handle_vehicle_gone_flushes_and_deletes_buffer(tmp_path):
    """vehicle_gone is the buffer-flush trigger for all track ends (drive-by
    + idle-leave). vehicle_left was the trigger pre-fix but it's now gated
    on idle_alerted in the tracker, so drive-by tracks wouldn't flush."""
    from services.vehicle_attributes.buffer import TrackBuffer
    buf = TrackBuffer(track_id="vehicle_0042", camera_id="cam1",
                      first_seen=1779394901.5)
    buf.append(crop=b"\xff\xd8jpeg", yolo_conf=0.85, bbox=[10, 20, 50, 60])
    buffers = {"vehicle_0042": buf}

    event = {
        "event_type": "vehicle_gone",
        "vehicle_id": "vehicle_0042",
        "camera_id": "cam1",
        "timestamp": "1779394907.2",
        "vehicle_class": "car",
        "was_idle": "False",
    }
    handle_event(event, buffers, r_bin=None, hd_size=(2304, 1296),
                 snapshot_root=str(tmp_path))
    assert "vehicle_0042" not in buffers
    track_dir = tmp_path / "cam1" / "2026-05-21" / "vehicle_0042_1779394901"
    assert (track_dir / "hero.jpg").is_file()
    assert (track_dir / "metadata.json").is_file()
    # was_idle=False → event_kind=drive_by
    import json
    meta = json.loads((track_dir / "metadata.json").read_text())
    assert meta["event_kind"] == "drive_by"


def test_handle_vehicle_gone_with_was_idle_records_idle_kind(tmp_path):
    """vehicle_gone with was_idle=True → metadata.event_kind=idle. This is
    the idle-leave case where vehicle_gone AND vehicle_left both fire."""
    from services.vehicle_attributes.buffer import TrackBuffer
    buf = TrackBuffer(track_id="vehicle_0050", camera_id="cam1",
                      first_seen=1779394901.5)
    buf.append(crop=b"\xff\xd8jpeg", yolo_conf=0.85, bbox=[10, 20, 50, 60])
    buffers = {"vehicle_0050": buf}

    event = {
        "event_type": "vehicle_gone",
        "vehicle_id": "vehicle_0050",
        "camera_id": "cam1",
        "timestamp": "1779394907.2",
        "vehicle_class": "car",
        "was_idle": "True",
    }
    handle_event(event, buffers, r_bin=None, hd_size=(2304, 1296),
                 snapshot_root=str(tmp_path))
    import json
    meta = json.loads(
        (tmp_path / "cam1" / "2026-05-21" / "vehicle_0050_1779394901" / "metadata.json")
        .read_text()
    )
    assert meta["event_kind"] == "idle"


def test_handle_vehicle_left_no_longer_flushes(tmp_path):
    """vehicle_left is now user-facing-only (gated on idle_alerted in the
    tracker). The attribute service must NOT flush on it — that's
    vehicle_gone's job. If both fire (idle-leave case), the buffer is
    already drained by vehicle_gone's prior flush; vehicle_left arriving
    later finds no buffer to flush, which is the correct no-op."""
    from services.vehicle_attributes.buffer import TrackBuffer
    buf = TrackBuffer(track_id="vehicle_0099", camera_id="cam1",
                      first_seen=1779394901.5)
    buf.append(crop=b"data", yolo_conf=0.7, bbox=[1, 2, 3, 4])
    buffers = {"vehicle_0099": buf}

    event = {
        "event_type": "vehicle_left",
        "vehicle_id": "vehicle_0099",
        "camera_id": "cam1",
        "timestamp": "1779394907.2",
        "vehicle_class": "car",
    }
    handle_event(event, buffers, r_bin=None, hd_size=(2304, 1296),
                 snapshot_root=str(tmp_path))
    # Buffer NOT drained — vehicle_left is no longer the flush trigger
    assert "vehicle_0099" in buffers
    # No directory created
    assert not (tmp_path / "cam1").exists()


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


def test_flush_does_not_call_classifier_when_disabled(monkeypatch, tmp_path):
    """ENABLE_CLASSIFIER=0 (default): _flush writes null-attributes metadata,
    classifier module is NOT called."""
    from services.vehicle_attributes import service as svc
    from services.vehicle_attributes.buffer import TrackBuffer

    monkeypatch.setenv("ENABLE_CLASSIFIER", "0")
    import importlib
    importlib.reload(svc)

    buf = TrackBuffer(track_id="vG", camera_id="cam1", first_seen=1779394901.5)
    buf.append(crop=b"\xff\xd8jpg", yolo_conf=0.85, bbox=[10, 20, 50, 60])
    buffers = {"vG": buf}

    classifier_called = {'flag': False}
    def _trip(*_a, **_k):
        classifier_called['flag'] = True
        return {}
    # Patch on the REAL dashed-name classifier module (mirror of Task 8's pattern)
    import services.vehicle_attributes  # primes sys.path
    import classifier
    monkeypatch.setattr(classifier, "run_classifier_and_vote", _trip)

    event = {
        "event_type": "vehicle_gone",
        "vehicle_id": "vG",
        "camera_id": "cam1",
        "timestamp": "1779394907.2",
        "vehicle_class": "car",
        "was_idle": "False",
    }
    svc.handle_event(event, buffers, r_bin=None, hd_size=(2304, 1296),
                     snapshot_root=str(tmp_path))

    assert classifier_called['flag'] is False
    import json as _json
    meta = _json.loads(
        (tmp_path / 'cam1' / '2026-05-21' / 'vG_1779394901' / 'metadata.json').read_text()
    )
    assert meta['attributes']['color'] is None


def test_flush_calls_classifier_when_enabled(monkeypatch, tmp_path):
    from services.vehicle_attributes import service as svc
    from services.vehicle_attributes.buffer import TrackBuffer

    monkeypatch.setenv("ENABLE_CLASSIFIER", "1")
    import importlib
    importlib.reload(svc)

    buf = TrackBuffer(track_id="vH", camera_id="cam1", first_seen=1779394901.5)
    buf.append(crop=b"\xff\xd8jpg", yolo_conf=0.85, bbox=[10, 20, 50, 60])
    buffers = {"vH": buf}

    expected_attrs = {
        'color': 'red', 'color_confidence': 0.8,
        'body_type': 'sedan', 'body_type_confidence': 0.75,
        'make': 'Honda', 'make_confidence': 0.7,
        'model': None, 'model_confidence': None,
        'voting_samples': 1,
        'classifier_version': 'v0-test-2026-05-21',
    }
    import services.vehicle_attributes  # primes sys.path
    import classifier
    monkeypatch.setattr(classifier, "run_classifier_and_vote",
                        lambda _buf, _kind: expected_attrs)

    event = {
        "event_type": "vehicle_gone",
        "vehicle_id": "vH",
        "camera_id": "cam1",
        "timestamp": "1779394907.2",
        "vehicle_class": "car",
        "was_idle": "False",
    }
    svc.handle_event(event, buffers, r_bin=None, hd_size=(2304, 1296),
                     snapshot_root=str(tmp_path))

    import json as _json
    meta = _json.loads(
        (tmp_path / 'cam1' / '2026-05-21' / 'vH_1779394901' / 'metadata.json').read_text()
    )
    assert meta['attributes']['color'] == 'red'
    assert meta['attributes']['make'] == 'Honda'


def test_flush_writes_embedding_npy_when_classifier_returns_one(monkeypatch, tmp_path):
    """Phase C1 — the classifier returns an `embedding` (768-dim numpy
    array) inside the attributes dict. storage.py pops it out and saves
    it as `embedding.npy` next to `hero.jpg`. The metadata.json's
    `attributes` block must NOT contain the embedding (it's an array,
    would bloat the JSON)."""
    import numpy as np
    from services.vehicle_attributes import service as svc
    from services.vehicle_attributes.buffer import TrackBuffer

    monkeypatch.setenv("ENABLE_CLASSIFIER", "1")
    import importlib
    importlib.reload(svc)

    buf = TrackBuffer(track_id="vEMB", camera_id="cam1", first_seen=1779394901.5)
    buf.append(crop=b"\xff\xd8jpg", yolo_conf=0.85, bbox=[10, 20, 50, 60])
    buffers = {"vEMB": buf}

    fake_embedding = np.arange(768, dtype="float32") / 768.0
    fake_embedding /= np.linalg.norm(fake_embedding)  # L2-normalize
    expected_attrs = {
        'color': 'gray', 'color_confidence': 0.7,
        'body_type': 'sedan', 'body_type_confidence': 0.7,
        'make': 'Honda', 'make_confidence': 0.65,
        'model': None, 'model_confidence': None,
        'voting_samples': 1,
        'classifier_version': 'v0-test-emb',
        'embedding': fake_embedding,
    }
    import services.vehicle_attributes  # primes sys.path
    import classifier
    monkeypatch.setattr(classifier, "run_classifier_and_vote",
                        lambda _buf, _kind: expected_attrs)

    event = {
        "event_type": "vehicle_gone",
        "vehicle_id": "vEMB",
        "camera_id": "cam1",
        "timestamp": "1779394907.2",
        "vehicle_class": "car",
        "was_idle": "False",
    }
    svc.handle_event(event, buffers, r_bin=None, hd_size=(2304, 1296),
                     snapshot_root=str(tmp_path))

    track_dir = tmp_path / 'cam1' / '2026-05-21' / 'vEMB_1779394901'
    emb_path = track_dir / 'embedding.npy'
    assert emb_path.is_file(), "embedding.npy must be written"

    loaded = np.load(emb_path)
    assert loaded.shape == (768,)
    assert loaded.dtype == np.float32
    np.testing.assert_array_almost_equal(loaded, fake_embedding, decimal=5)

    # metadata.json's attributes block must NOT contain the embedding
    import json as _json
    meta = _json.loads((track_dir / 'metadata.json').read_text())
    assert 'embedding' not in meta['attributes'], \
        "embedding must be popped from attributes before JSON serialization"


def test_flush_skips_embedding_when_classifier_disabled(monkeypatch, tmp_path):
    """No classifier → no embedding key in attributes → no embedding.npy
    file. The metadata.json gets the null-attributes block."""
    from services.vehicle_attributes import service as svc
    from services.vehicle_attributes.buffer import TrackBuffer

    monkeypatch.setenv("ENABLE_CLASSIFIER", "0")
    import importlib
    importlib.reload(svc)

    buf = TrackBuffer(track_id="vNO", camera_id="cam1", first_seen=1779394901.5)
    buf.append(crop=b"\xff\xd8jpg", yolo_conf=0.85, bbox=[10, 20, 50, 60])
    buffers = {"vNO": buf}

    event = {
        "event_type": "vehicle_gone",
        "vehicle_id": "vNO",
        "camera_id": "cam1",
        "timestamp": "1779394907.2",
        "vehicle_class": "car",
        "was_idle": "False",
    }
    svc.handle_event(event, buffers, r_bin=None, hd_size=(2304, 1296),
                     snapshot_root=str(tmp_path))

    track_dir = tmp_path / 'cam1' / '2026-05-21' / 'vNO_1779394901'
    assert not (track_dir / 'embedding.npy').exists(), \
        "no embedding.npy expected when classifier disabled"
