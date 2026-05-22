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
    target = tmp_path / "cam1" / "2026-05-21" / "vehicle_0042_1779394901"
    assert target.is_dir()


def test_flush_writes_hero_and_angles(tmp_path):
    b = _seeded_buffer(3)
    flush_buffer_to_disk(b, last_seen=1779394907.2, event_kind="drive_by",
                         vehicle_class="car",
                         snapshot_root=str(tmp_path))
    track_dir = tmp_path / "cam1" / "2026-05-21" / "vehicle_0042_1779394901"
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
        (tmp_path / "cam1" / "2026-05-21" / "vehicle_0042_1779394901" / "metadata.json")
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


def test_flush_writes_attributes_when_provided(tmp_path):
    b = _seeded_buffer(2)
    attrs = {
        'color': 'red',
        'color_confidence': 0.82,
        'body_type': 'sedan',
        'body_type_confidence': 0.78,
        'make': 'Honda',
        'make_confidence': 0.71,
        'model': 'Civic',
        'model_confidence': 0.68,
        'voting_samples': 2,
        'classifier_version': 'v0-convnext_tiny_v0-2026-05-21',
    }
    flush_buffer_to_disk(b, last_seen=1779394907.2, event_kind='drive_by',
                         vehicle_class='car', snapshot_root=str(tmp_path),
                         attributes=attrs)
    track_dir = tmp_path / 'cam1' / '2026-05-21' / 'vehicle_0042_1779394901'
    meta = json.loads((track_dir / 'metadata.json').read_text())
    assert meta['attributes']['color'] == 'red'
    assert meta['attributes']['make'] == 'Honda'
    assert meta['attributes']['model'] == 'Civic'
    assert meta['attributes']['classifier_version'].startswith('v0-')


def test_flush_two_different_cars_with_reused_track_id_do_not_collide(tmp_path):
    """Contract: when tracker-cam{N} restarts, `_next_vehicle_id` resets
    to 1 and a brand-new physical vehicle gets minted as `vehicle_0001`
    again. If two such tracks flush on the same day, the dir name must
    NOT collide — otherwise the second flush silently overwrites the
    first car's hero.jpg + metadata.json (real data loss). Naming dirs
    by `<track_id>_<first_seen_epoch>` keeps them distinct."""
    # Vehicle 1: parked Honda, first_seen 1779462229
    b1 = TrackBuffer(track_id="vehicle_0001", camera_id="cam1",
                     first_seen=1779462229.0)
    b1.append(crop=bytes([0xFF, 0xD8]) + b"honda", yolo_conf=0.9,
              bbox=[100, 100, 200, 200])
    flush_buffer_to_disk(b1, last_seen=1779462250.0, event_kind="idle",
                         vehicle_class="car",
                         snapshot_root=str(tmp_path))

    # Vehicle 2: same id reused after a hypothetical tracker restart;
    # different physical car, first_seen 2 minutes later.
    b2 = TrackBuffer(track_id="vehicle_0001", camera_id="cam1",
                     first_seen=1779462349.0)
    b2.append(crop=bytes([0xFF, 0xD8]) + b"ford", yolo_conf=0.9,
              bbox=[300, 100, 400, 200])
    flush_buffer_to_disk(b2, last_seen=1779462360.0, event_kind="drive_by",
                         vehicle_class="truck",
                         snapshot_root=str(tmp_path))

    day_dir = tmp_path / "cam1" / "2026-05-22"
    dirs = sorted(d.name for d in day_dir.iterdir() if d.is_dir())
    # Both flushed dirs must exist with distinct names.
    assert dirs == ["vehicle_0001_1779462229", "vehicle_0001_1779462349"], dirs
    # First car's hero is preserved.
    assert (day_dir / "vehicle_0001_1779462229" / "hero.jpg").read_bytes() \
        == bytes([0xFF, 0xD8]) + b"honda"
    # Second car's hero is also there.
    assert (day_dir / "vehicle_0001_1779462349" / "hero.jpg").read_bytes() \
        == bytes([0xFF, 0xD8]) + b"ford"
    # metadata.json for each still carries the friendly track_id.
    meta1 = json.loads((day_dir / "vehicle_0001_1779462229" / "metadata.json").read_text())
    meta2 = json.loads((day_dir / "vehicle_0001_1779462349" / "metadata.json").read_text())
    assert meta1["track_id"] == "vehicle_0001"
    assert meta2["track_id"] == "vehicle_0001"
    assert meta1["vehicle_class"] == "car"
    assert meta2["vehicle_class"] == "truck"


def test_flush_omitted_attributes_keeps_phase1_null_block(tmp_path):
    b = _seeded_buffer(2)
    flush_buffer_to_disk(b, last_seen=1779394907.2, event_kind='drive_by',
                         vehicle_class='car', snapshot_root=str(tmp_path))
    track_dir = tmp_path / 'cam1' / '2026-05-21' / 'vehicle_0042_1779394901'
    meta = json.loads((track_dir / 'metadata.json').read_text())
    assert meta['attributes']['color'] is None
    assert meta['attributes']['body_type'] is None
    assert meta['attributes']['make'] is None
    assert meta['attributes']['model'] is None
