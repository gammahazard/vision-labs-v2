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
    track_dir = tmp_path / "cam1" / "2026-05-21" / "vehicle_0042"
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
        (tmp_path / "cam1" / "2026-05-21" / "vehicle_0050" / "metadata.json")
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
        (tmp_path / 'cam1' / '2026-05-21' / 'vG' / 'metadata.json').read_text()
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
        (tmp_path / 'cam1' / '2026-05-21' / 'vH' / 'metadata.json').read_text()
    )
    assert meta['attributes']['color'] == 'red'
    assert meta['attributes']['make'] == 'Honda'
