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
    """Reservoir sampling: buffer always at max_crops once seen > max, and
    the kept samples are spread across the input sequence (not just first N).

    With random.seed(42), the reservoir produces a deterministic-but-spread
    sample set — exact indices don't matter, but they must NOT be [0,1,2]
    (which is what first-N would keep)."""
    import random
    random.seed(42)
    b = TrackBuffer(track_id="v", camera_id="cam1", first_seen=0.0,
                    max_crops=3)
    for i in range(10):
        b.append(crop=bytes([i]), yolo_conf=0.5, bbox=[0, 0, 1, 1])
    assert len(b.crops) == 3, "buffer must stay at cap"
    assert b.is_full() is True
    kept_bytes = sorted(int(c[0]) for c in b.crops)
    assert kept_bytes != [0, 1, 2], (
        "first-N retention would be [0,1,2]; reservoir picks spread samples"
    )


def test_buffer_reservoir_sampling_is_statistically_uniform():
    """Across many runs, each input index has ~equal probability of being
    in the final reservoir."""
    import random
    n_runs = 200
    max_crops = 3
    n_inputs = 10
    sum_kept_indices = 0
    for run in range(n_runs):
        random.seed(run)
        b = TrackBuffer(track_id="v", camera_id="cam1", first_seen=0.0,
                        max_crops=max_crops)
        for i in range(n_inputs):
            b.append(crop=bytes([i]), yolo_conf=0.5, bbox=[0, 0, 1, 1])
        sum_kept_indices += sum(int(c[0]) for c in b.crops)
    expected = n_runs * max_crops * (n_inputs - 1) / 2
    observed = sum_kept_indices
    assert 0.8 * expected < observed < 1.2 * expected, (
        f"reservoir mean drift: expected ~{expected}, got {observed}"
    )


def test_buffer_hero_index_picks_highest_confidence():
    b = TrackBuffer(track_id="v", camera_id="cam1", first_seen=0.0)
    b.append(crop=b"a", yolo_conf=0.6, bbox=[0, 0, 1, 1])
    b.append(crop=b"b", yolo_conf=0.9, bbox=[0, 0, 1, 1])
    b.append(crop=b"c", yolo_conf=0.75, bbox=[0, 0, 1, 1])
    assert b.hero_index() == 1


def test_buffer_hero_index_none_when_empty():
    b = TrackBuffer(track_id="v", camera_id="cam1", first_seen=0.0)
    assert b.hero_index() is None
