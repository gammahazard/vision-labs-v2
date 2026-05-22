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
