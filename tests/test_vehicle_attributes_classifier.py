"""Unit tests for vehicle-attributes classifier.py."""
import io
import json
import pytest
import numpy as np
from PIL import Image

# torch ships inside the vehicle-attributes Docker image but not in the host
# test venv that CI runs against. Skip cleanly when absent.
pytest.importorskip("torch")


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


def test_consistency_keeps_matching_pair():
    from services.vehicle_attributes.classifier import _enforce_make_model_consistency
    make_to_models = {
        'Honda': ['Civic', 'Accord'],
        'Toyota': ['Camry', 'Corolla'],
    }
    new_make, new_model = _enforce_make_model_consistency(
        ('Honda', 0.8), ('Civic', 0.7), make_to_models,
    )
    assert new_make == ('Honda', 0.8)
    assert new_model == ('Civic', 0.7)


def test_consistency_drops_model_when_less_confident():
    from services.vehicle_attributes.classifier import _enforce_make_model_consistency
    make_to_models = {'Honda': ['Civic'], 'Toyota': ['Camry']}
    new_make, new_model = _enforce_make_model_consistency(
        ('Toyota', 0.8), ('Civic', 0.6), make_to_models,
    )
    assert new_make == ('Toyota', 0.8)
    assert new_model == (None, 0.6)


def test_consistency_drops_make_when_less_confident():
    from services.vehicle_attributes.classifier import _enforce_make_model_consistency
    make_to_models = {'Honda': ['Civic']}
    new_make, new_model = _enforce_make_model_consistency(
        ('Toyota', 0.4), ('Civic', 0.7), make_to_models,
    )
    assert new_make == (None, 0.4)
    assert new_model == ('Civic', 0.7)


def test_consistency_skips_when_either_is_none():
    from services.vehicle_attributes.classifier import _enforce_make_model_consistency
    make_to_models = {'Honda': ['Civic']}
    new_make, new_model = _enforce_make_model_consistency(
        ('Toyota', 0.8), (None, 0.4), make_to_models,
    )
    assert new_make == ('Toyota', 0.8)
    assert new_model == (None, 0.4)
    new_make, new_model = _enforce_make_model_consistency(
        (None, 0.4), ('Civic', 0.8), make_to_models,
    )
    assert new_make == (None, 0.4)
    assert new_model == ('Civic', 0.8)


def _mock_color_model(num_crops: int):
    """Mimics the color model's forward pass — returns logits over 10 colors."""
    import torch
    def fake_forward(_x):
        out = torch.zeros(num_crops, 10)
        out[:, 4] = 10.0   # 'red'
        return out
    return fake_forward


def _mock_multihead(num_crops: int):
    """Mimics the multi-head model's forward pass — returns 3 logit tensors."""
    import torch
    def fake_forward(_x):
        out = {
            'body':  torch.zeros(num_crops, 8),
            'make':  torch.zeros(num_crops, 50),
            'model': torch.zeros(num_crops, 196),
        }
        # Logit magnitude chosen so the 196-class softmax for model crosses
        # the 0.65 threshold from the spec. Smaller heads cross sooner.
        out['body'][:, 0] = 10.0    # 'sedan'
        out['make'][:, 21] = 10.0   # 'Honda' (per the mock classes below)
        out['model'][:, 50] = 10.0  # arbitrary model index
        return out
    return fake_forward


def test_run_classifier_and_vote_drive_by_predicts_all_four(monkeypatch):
    import services.vehicle_attributes  # ensure sys.path is set up  # noqa: F401
    import classifier as clf
    from services.vehicle_attributes.buffer import TrackBuffer
    import torch

    monkeypatch.setattr(clf, "_load_color_model",
                        lambda: _mock_color_model(num_crops=3))
    monkeypatch.setattr(clf, "_load_multihead_model",
                        lambda: _mock_multihead(num_crops=3))
    monkeypatch.setattr(clf, "_is_monochrome", lambda _b: False)
    monkeypatch.setattr(clf, "_load_classes", lambda: {
        'color': ['yellow','orange','green','gray','red','blue','white','golden','brown','black'],
        'body':  ['sedan','suv','coupe','pickup','van','hatchback','convertible','wagon'],
        'make':  ['Acura'] * 21 + ['Honda'] + ['Toyota'] * 28,
        'model': [f'm{i}' for i in range(196)],
        'make_to_models': {'Honda': ['m50']},
    })
    monkeypatch.setattr(clf, "_preprocess",
                        lambda crops: torch.zeros(len(crops), 3, 224, 224))

    buf = TrackBuffer(track_id="v_drive", camera_id="cam1", first_seen=0.0)
    for _ in range(3):
        buf.append(crop=_make_jpeg(np.zeros((100, 80, 3), dtype=np.uint8)),
                   yolo_conf=0.8, bbox=[10, 20, 50, 60])

    out = clf.run_classifier_and_vote(buf, event_kind="drive_by")
    assert out['color'] == 'red'
    assert out['color_confidence'] is not None
    assert out['body_type'] == 'sedan'
    assert out['make'] == 'Honda'
    assert out['model'] == 'm50'
    assert out['voting_samples'] == 3
    assert out['classifier_version'].startswith('v0-')
    assert out['ir_track'] is False


def test_run_classifier_and_vote_idle_event_still_runs_all_heads(monkeypatch):
    import services.vehicle_attributes  # ensure sys.path is set up  # noqa: F401
    import classifier as clf
    from services.vehicle_attributes.buffer import TrackBuffer
    import torch

    monkeypatch.setattr(clf, "_load_color_model",
                        lambda: _mock_color_model(num_crops=2))
    monkeypatch.setattr(clf, "_load_multihead_model",
                        lambda: _mock_multihead(num_crops=2))
    monkeypatch.setattr(clf, "_is_monochrome", lambda _b: False)
    monkeypatch.setattr(clf, "_load_classes", lambda: {
        'color': ['yellow','orange','green','gray','red','blue','white','golden','brown','black'],
        'body':  ['sedan','suv','coupe','pickup','van','hatchback','convertible','wagon'],
        'make':  ['Acura'] * 21 + ['Honda'] + ['Toyota'] * 28,
        'model': [f'm{i}' for i in range(196)],
        'make_to_models': {'Honda': ['m50']},
    })
    monkeypatch.setattr(clf, "_preprocess",
                        lambda crops: torch.zeros(len(crops), 3, 224, 224))

    buf = TrackBuffer(track_id="v_idle", camera_id="cam1", first_seen=0.0)
    for _ in range(2):
        buf.append(crop=_make_jpeg(np.zeros((100, 80, 3), dtype=np.uint8)),
                   yolo_conf=0.8, bbox=[10, 20, 50, 60])

    out = clf.run_classifier_and_vote(buf, event_kind="idle")
    assert out['color'] == 'red'
    assert out['body_type'] == 'sedan'
    assert out['make'] == 'Honda'
    # Model head now runs on idle too — parked cars give well-sampled
    # multi-angle views and a model prediction is signal worth showing.
    assert out['model'] == 'm50'
    assert out['model_confidence'] is not None


def test_run_classifier_and_vote_below_threshold_shows_confidence(monkeypatch):
    """Contract: when a head VOTES but its winning class is below the
    threshold, the label is None but the confidence still reports the
    losing value. Previously color + model nulled out the confidence too,
    which made it impossible to see 'color was 0.53, just barely under
    the 0.55 threshold' vs 'color was 0.18, way off'. Body + make already
    behaved this way; this test locks in the same for color + model."""
    import services.vehicle_attributes  # noqa: F401
    import classifier as clf
    from services.vehicle_attributes.buffer import TrackBuffer
    import torch

    def _below_threshold_color(num_crops):
        """Color fake whose softmax winner sits well below 0.55."""
        def fake(_x):
            out = torch.full((num_crops, 10), 0.5)
            out[:, 0] = 1.5  # mildly favors 'yellow' but vote stays ~0.40
            return out
        return fake

    monkeypatch.setattr(clf, "_load_color_model",
                        lambda: _below_threshold_color(num_crops=2))
    monkeypatch.setattr(clf, "_load_multihead_model",
                        lambda: _mock_multihead(num_crops=2))
    monkeypatch.setattr(clf, "_is_monochrome", lambda _b: False)
    monkeypatch.setattr(clf, "_load_classes", lambda: {
        'color': ['yellow','orange','green','gray','red','blue','white','golden','brown','black'],
        'body':  ['sedan','suv','coupe','pickup','van','hatchback','convertible','wagon'],
        'make':  ['Acura'] * 21 + ['Honda'] + ['Toyota'] * 28,
        'model': [f'm{i}' for i in range(196)],
        'make_to_models': {'Honda': ['m50']},
    })
    monkeypatch.setattr(clf, "_preprocess",
                        lambda crops: torch.zeros(len(crops), 3, 224, 224))

    buf = TrackBuffer(track_id="v_low_conf", camera_id="cam1", first_seen=0.0)
    for _ in range(2):
        buf.append(crop=_make_jpeg(np.zeros((100, 80, 3), dtype=np.uint8)),
                   yolo_conf=0.8, bbox=[10, 20, 50, 60])

    out = clf.run_classifier_and_vote(buf, event_kind="drive_by")
    # Color voted but lost: label is None, conf is NOW reported (the fix).
    assert out['color'] is None
    assert out['color_confidence'] is not None
    assert 0.0 < out['color_confidence'] < 0.55


def test_run_classifier_and_vote_empty_buffer_returns_all_null(monkeypatch):
    import services.vehicle_attributes  # ensure sys.path is set up  # noqa: F401
    import classifier as clf
    from services.vehicle_attributes.buffer import TrackBuffer

    monkeypatch.setattr(clf, "_load_color_model", lambda: None)
    monkeypatch.setattr(clf, "_load_multihead_model", lambda: None)
    monkeypatch.setattr(clf, "_is_monochrome", lambda _b: False)
    monkeypatch.setattr(clf, "_load_classes", lambda: {
        'color': [], 'body': [], 'make': [], 'model': [], 'make_to_models': {},
    })

    buf = TrackBuffer(track_id="v_empty", camera_id="cam1", first_seen=0.0)
    out = clf.run_classifier_and_vote(buf, event_kind="drive_by")
    assert out['color'] is None
    assert out['body_type'] is None
    assert out['make'] is None
    assert out['model'] is None
    assert out['voting_samples'] == 0


def test_run_classifier_and_vote_ir_track_skips_color(monkeypatch):
    """IR/night-vision tracks: color is suppressed, make/body/model still run."""
    import services.vehicle_attributes  # noqa: F401
    import classifier as clf
    from services.vehicle_attributes.buffer import TrackBuffer
    import torch

    monkeypatch.setattr(clf, "_load_color_model",
                        lambda: _mock_color_model(num_crops=3))
    monkeypatch.setattr(clf, "_load_multihead_model",
                        lambda: _mock_multihead(num_crops=3))
    # Force every crop to be classified as monochrome → IR-track path.
    monkeypatch.setattr(clf, "_is_monochrome", lambda _b: True)
    monkeypatch.setattr(clf, "_load_classes", lambda: {
        'color': ['yellow','orange','green','gray','red','blue','white','golden','brown','black'],
        'body':  ['sedan','suv','coupe','pickup','van','hatchback','convertible','wagon'],
        'make':  ['Acura'] * 21 + ['Honda'] + ['Toyota'] * 28,
        'model': [f'm{i}' for i in range(196)],
        'make_to_models': {'Honda': ['m50']},
    })
    monkeypatch.setattr(clf, "_preprocess",
                        lambda crops: torch.zeros(len(crops), 3, 224, 224))

    buf = TrackBuffer(track_id="v_ir", camera_id="cam1", first_seen=0.0)
    for _ in range(3):
        buf.append(crop=_make_jpeg(np.zeros((100, 80, 3), dtype=np.uint8)),
                   yolo_conf=0.8, bbox=[10, 20, 50, 60])

    out = clf.run_classifier_and_vote(buf, event_kind="drive_by")
    assert out['color'] is None
    assert out['color_confidence'] is None
    assert out['ir_track'] is True
    # Multi-head heads still emit predictions on IR frames.
    assert out['body_type'] == 'sedan'
    assert out['make'] == 'Honda'
    assert out['model'] == 'm50'
