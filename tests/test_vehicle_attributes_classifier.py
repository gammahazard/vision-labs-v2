"""Unit tests for vehicle-attributes classifier.py."""
import io
import json
import pytest
import numpy as np
from PIL import Image


def _make_jpeg(rgb_arr: np.ndarray) -> bytes:
    img = Image.fromarray(rgb_arr)
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=85)
    return buf.getvalue()


def test_preprocess_decodes_jpegs_to_batched_tensor():
    from services.vehicle_attributes.classifier import _preprocess
    # value 0 (black) normalizes to ~-2.1 on all channels; value 200 normalizes to ~+2.5
    arr_a = np.zeros((100, 80, 3), dtype=np.uint8)
    arr_b = np.zeros((50, 50, 3), dtype=np.uint8) + 200
    jpegs = [_make_jpeg(arr_a), _make_jpeg(arr_b)]
    t = _preprocess(jpegs)
    assert tuple(t.shape) == (2, 3, 224, 224)
    assert -3.0 < float(t.min()) < 0.0
    assert 0.0 < float(t.max()) < 3.0


def test_preprocess_empty_list_returns_empty_tensor():
    from services.vehicle_attributes.classifier import _preprocess
    t = _preprocess([])
    assert tuple(t.shape) == (0, 3, 224, 224)


def test_preprocess_rejects_invalid_jpeg_gracefully():
    from services.vehicle_attributes.classifier import _preprocess
    valid = _make_jpeg(np.zeros((100, 80, 3), dtype=np.uint8))
    invalid = b"\x00\x01\x02 not a jpeg"
    t = _preprocess([valid, invalid, valid])
    assert tuple(t.shape) == (2, 3, 224, 224)


def test_vote_single_strong_prediction():
    import torch
    from services.vehicle_attributes.classifier import _vote
    classes = ['A', 'B', 'C']
    probs = torch.tensor([[0.05, 0.90, 0.05]])
    yolo_confs = [0.8]
    winner, conf = _vote(probs, yolo_confs, classes, threshold=0.55)
    assert winner == 'B'
    assert conf > 0.55


def test_vote_below_threshold_returns_none():
    import torch
    from services.vehicle_attributes.classifier import _vote
    classes = ['A', 'B', 'C']
    probs = torch.tensor([[0.4, 0.35, 0.25]])
    yolo_confs = [0.7]
    winner, conf = _vote(probs, yolo_confs, classes, threshold=0.55)
    assert winner is None
    assert conf < 0.55


def test_vote_weighted_by_yolo_confidence():
    import torch
    from services.vehicle_attributes.classifier import _vote
    classes = ['A', 'B']
    probs = torch.tensor([
        [0.95, 0.05],
        [0.55, 0.45],
    ])
    yolo_confs = [0.1, 0.95]
    winner, conf = _vote(probs, yolo_confs, classes, threshold=0.55)
    assert winner == 'A'


def test_vote_empty_input_returns_none():
    import torch
    from services.vehicle_attributes.classifier import _vote
    classes = ['A', 'B']
    probs = torch.empty(0, 2)
    yolo_confs = []
    winner, conf = _vote(probs, yolo_confs, classes, threshold=0.55)
    assert winner is None
    assert conf == 0.0
