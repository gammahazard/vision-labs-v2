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
