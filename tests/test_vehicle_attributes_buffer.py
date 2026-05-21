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
