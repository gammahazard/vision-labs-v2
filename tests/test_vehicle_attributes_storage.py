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
