"""ConvNeXt-Tiny multi-head classifier for vehicle attributes (Phase 3 v0).

Loaded lazily on first inference call so the service can boot quickly +
fail fast if HF Hub is unreachable. Single-process singleton.

Public entry: run_classifier_and_vote(buf, event_kind) -> dict
"""
import json
import logging
import os
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger("vehicle-attributes.classifier")


# ---------------------------------------------------------------------------
# Preprocessing (pure functions, no model state)
# ---------------------------------------------------------------------------

_IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


def _preprocess(jpeg_crops: list[bytes]):
    """Decode JPEGs → resize-with-aspect → center-crop 224×224 → normalize.

    Returns a (B, 3, 224, 224) torch.Tensor with ImageNet normalization
    applied. Corrupt JPEGs are silently dropped (single bad crop doesn't
    kill the whole flush).
    """
    import torch
    if not jpeg_crops:
        return torch.empty(0, 3, 224, 224)

    tensors = []
    for jpeg in jpeg_crops:
        try:
            arr = np.frombuffer(jpeg, dtype=np.uint8)
            bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if bgr is None:
                continue
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            h, w = rgb.shape[:2]
            short = min(h, w)
            scale = 224.0 / short
            new_h = max(224, int(round(h * scale)))
            new_w = max(224, int(round(w * scale)))
            rgb = cv2.resize(rgb, (new_w, new_h),
                             interpolation=cv2.INTER_AREA)
            y0 = (new_h - 224) // 2
            x0 = (new_w - 224) // 2
            cropped = rgb[y0:y0 + 224, x0:x0 + 224]
            arr_f = cropped.astype(np.float32) / 255.0
            arr_f = (arr_f - _IMAGENET_MEAN) / _IMAGENET_STD
            arr_f = np.transpose(arr_f, (2, 0, 1))
            tensors.append(torch.from_numpy(arr_f.copy()))
        except Exception as e:
            logger.debug(f"skipping crop in preprocess: {e}")
            continue

    if not tensors:
        return torch.empty(0, 3, 224, 224)
    return torch.stack(tensors)


def _vote(per_crop_probs, yolo_confs: list[float],
          classes: list[str], threshold: float):
    """Weighted majority vote across crops.
    Returns (winner_label_or_None, winner_confidence).
    """
    import torch
    if per_crop_probs.numel() == 0 or not yolo_confs:
        return (None, 0.0)
    yc = torch.tensor(yolo_confs, dtype=per_crop_probs.dtype,
                      device=per_crop_probs.device)
    weighted = (per_crop_probs * yc.unsqueeze(1)).sum(dim=0)
    total = weighted.sum()
    if total <= 0:
        return (None, 0.0)
    weighted = weighted / total
    winner_idx = int(weighted.argmax().item())
    winner_conf = float(weighted[winner_idx].item())
    if winner_conf < threshold:
        return (None, winner_conf)
    return (classes[winner_idx], winner_conf)


def _enforce_make_model_consistency(
    make_out: tuple,
    model_out: tuple,
    make_to_models: dict,
) -> tuple:
    """Drop the less-confident of (make, model) when the predicted model
    isn't in the predicted make's roster. No-op if either is None.
    """
    make_label, make_conf = make_out
    model_label, model_conf = model_out
    if make_label is None or model_label is None:
        return (make_out, model_out)
    if model_label in make_to_models.get(make_label, ()):
        return (make_out, model_out)
    if make_conf >= model_conf:
        return (make_out, (None, model_conf))
    return ((None, make_conf), model_out)


# ---------------------------------------------------------------------------
# Model + classes loading (singletons, lazy)
#
# v0 ships TWO models, not one:
#   - color_model: ImageNet-pretrained ConvNeXt-Tiny backbone (frozen) +
#     linear color head trained on VeRi-776.
#   - multihead_model: ConvNeXt-Tiny backbone fine-tuned on Stanford Cars-196
#     + body / make / model heads.
#
# Sharing one backbone across both didn't work: color training needed the
# generic ImageNet feature space (cheap, no overfit), and make/model needed
# a backbone specialized to fine-grained vehicles. Fine-tuning one backbone
# for cars would erase the color head's learned mapping. Two models cost
# ~1.2 GB VRAM total — well within the small-tier budget.
# ---------------------------------------------------------------------------

_COLOR_MODEL = None
_MULTIHEAD_MODEL = None
_CLASSES = None

MODELS_DIR = os.environ.get("VEHICLE_ATTR_MODELS_DIR", "/models")
HF_REPO = os.environ.get("VEHICLE_ATTR_HF_REPO",
                          "mangolover/vision-labs-vehicle-attributes")
COLOR_MODEL_NAME = os.environ.get(
    "VEHICLE_ATTR_COLOR_MODEL", "color_head_v0",
)
MULTIHEAD_MODEL_NAME = os.environ.get(
    "VEHICLE_ATTR_MULTIHEAD_MODEL", "multihead_v0",
)

COLOR_CONF = float(os.environ.get("COLOR_CONF_THRESHOLD", "0.55"))
BODY_CONF = float(os.environ.get("BODY_CONF_THRESHOLD", "0.55"))
MAKE_CONF = float(os.environ.get("MAKE_CONF_THRESHOLD", "0.55"))
MODEL_CONF = float(os.environ.get("MODEL_CONF_THRESHOLD", "0.65"))

# Saturation threshold under which a crop is treated as monochrome / IR.
# Skips color classification because all RGB channels are equal and the
# VeRi-776 head would just emit noise. Body/make/model still run — they
# rely on shape, not chroma.
IR_SATURATION_THRESHOLD = float(
    os.environ.get("IR_SATURATION_THRESHOLD", "8.0"),
)

MODEL_VERSION = f"v0-color={COLOR_MODEL_NAME}-multihead={MULTIHEAD_MODEL_NAME}"


def _classes_dir() -> Path:
    """The classes/ directory shipped in the container image."""
    return Path(__file__).resolve().parent / "classes"


def _load_classes() -> dict:
    """Read class label JSONs + make-to-models map. Cached singleton."""
    global _CLASSES
    if _CLASSES is not None:
        return _CLASSES
    d = _classes_dir()
    _CLASSES = {
        'color': json.loads((d / 'color_classes.json').read_text()),
        'body':  json.loads((d / 'body_classes.json').read_text()),
        'make':  json.loads((d / 'make_classes.json').read_text()),
        'model': json.loads((d / 'model_classes.json').read_text()),
        'make_to_models': json.loads((d / 'make_to_models.json').read_text()),
    }
    return _CLASSES


def _is_monochrome(jpeg_bytes: bytes) -> bool:
    """Detect IR / night-vision frames whose RGB channels are nearly equal.

    Color head trained on VeRi-776 sees uniformly low-saturation input and
    emits noise. Cheaper to skip color and return None than to pretend.
    """
    arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if bgr is None:
        return False
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    return float(hsv[..., 1].mean()) < IR_SATURATION_THRESHOLD


def _build_color_model():
    """ImageNet-pretrained ConvNeXt-Tiny + linear color head (no learned
    backbone weights here — backbone stays at its in22k-ft-in1k state).
    """
    import torch.nn as nn
    import timm
    classes = _load_classes()
    backbone = timm.create_model(
        'convnext_tiny', pretrained=True, num_classes=0,
    )
    feat_dim = backbone.num_features

    class ColorModel(nn.Module):
        def __init__(self):
            super().__init__()
            self.backbone = backbone
            self.color_head = nn.Linear(feat_dim, len(classes['color']))

        def forward(self, x):
            return self.color_head(self.backbone(x))
    return ColorModel()


def _build_multihead_model():
    """ConvNeXt-Tiny + body/make/model heads. Backbone weights come from
    the fine-tuned Stanford-Cars checkpoint, NOT ImageNet — pretrained=False
    here so we don't waste a download replacing weights we're about to load.
    """
    import torch.nn as nn
    import timm
    classes = _load_classes()
    backbone = timm.create_model(
        'convnext_tiny', pretrained=False, num_classes=0,
    )
    feat_dim = backbone.num_features

    class MultiHeadModel(nn.Module):
        def __init__(self):
            super().__init__()
            self.backbone = backbone
            self.body_head = nn.Linear(feat_dim, len(classes['body']))
            self.make_head = nn.Linear(feat_dim, len(classes['make']))
            self.model_head = nn.Linear(feat_dim, len(classes['model']))

        def forward(self, x):
            feats = self.backbone(x)
            return {
                'body':  self.body_head(feats),
                'make':  self.make_head(feats),
                'model': self.model_head(feats),
            }
    return MultiHeadModel()


def _download_weights(filename: str) -> Path:
    """Download `<filename>.safetensors` from HF Hub into MODELS_DIR if
    missing, return the local path.
    """
    from huggingface_hub import hf_hub_download

    target_dir = Path(MODELS_DIR)
    target_dir.mkdir(parents=True, exist_ok=True)
    weights_path = target_dir / f"{filename}.safetensors"
    if not weights_path.exists():
        logger.info(f"downloading {filename} weights from {HF_REPO}")
        downloaded = hf_hub_download(
            repo_id=HF_REPO,
            filename=f"{filename}.safetensors",
            local_dir=str(target_dir),
        )
        if Path(downloaded) != weights_path:
            os.replace(downloaded, weights_path)
    return weights_path


def _load_color_model():
    """Lazy-load the color model singleton."""
    global _COLOR_MODEL
    if _COLOR_MODEL is not None:
        return _COLOR_MODEL
    import torch
    from safetensors.torch import load_file
    weights = _download_weights(COLOR_MODEL_NAME)
    model = _build_color_model()
    state = load_file(str(weights))
    # color_head.safetensors only contains the linear-head tensors; missing
    # backbone params keep their ImageNet-pretrained values.
    missing, unexpected = model.load_state_dict(state, strict=False)
    if unexpected:
        logger.warning(f"color model: unexpected keys {unexpected}")
    model.train(False)
    if torch.cuda.is_available():
        model = model.cuda()
    _COLOR_MODEL = model
    logger.info(
        f"loaded color model {COLOR_MODEL_NAME} "
        f"({'cuda' if torch.cuda.is_available() else 'cpu'})"
    )
    return _COLOR_MODEL


def _load_multihead_model():
    """Lazy-load the body/make/model singleton."""
    global _MULTIHEAD_MODEL
    if _MULTIHEAD_MODEL is not None:
        return _MULTIHEAD_MODEL
    import torch
    from safetensors.torch import load_file
    weights = _download_weights(MULTIHEAD_MODEL_NAME)
    model = _build_multihead_model()
    state = load_file(str(weights))
    model.load_state_dict(state)
    model.train(False)
    if torch.cuda.is_available():
        model = model.cuda()
    _MULTIHEAD_MODEL = model
    logger.info(
        f"loaded multi-head model {MULTIHEAD_MODEL_NAME} "
        f"({'cuda' if torch.cuda.is_available() else 'cpu'})"
    )
    return _MULTIHEAD_MODEL


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def run_classifier_and_vote(buf, event_kind: str) -> dict:
    """Run the color + multi-head classifiers across all crops in the buffer,
    apply voting + thresholds + make-model consistency, return attributes
    dict for storage.py to merge into metadata.json.

    Two model forward passes per flush (one per backbone). Color is skipped
    on IR/monochrome frames — the head was trained on saturated VeRi-776
    crops and would just emit noise on night-vision input.
    """
    import torch

    classes = _load_classes()

    if not buf.crops:
        return {
            'color': None, 'color_confidence': None,
            'body_type': None, 'body_type_confidence': None,
            'make': None, 'make_confidence': None,
            'model': None, 'model_confidence': None,
            'voting_samples': 0,
            'classifier_version': MODEL_VERSION,
        }

    crops_t = _preprocess(buf.crops)
    if crops_t.numel() == 0:
        return {
            'color': None, 'color_confidence': None,
            'body_type': None, 'body_type_confidence': None,
            'make': None, 'make_confidence': None,
            'model': None, 'model_confidence': None,
            'voting_samples': 0,
            'classifier_version': MODEL_VERSION,
        }
    if torch.cuda.is_available():
        crops_t = crops_t.cuda()

    # Treat the track as IR if MORE THAN HALF of its crops are monochrome.
    # One-off camera glare on a daytime frame shouldn't suppress color.
    n_mono = sum(1 for c in buf.crops if _is_monochrome(c))
    is_ir_track = n_mono > len(buf.crops) // 2

    # Color path — skip entirely on IR tracks.
    if is_ir_track:
        color_out = (None, 0.0)
    else:
        color_model = _load_color_model()
        with torch.inference_mode():
            color_logits = color_model(crops_t)
        color_probs = torch.softmax(color_logits, dim=1)
        color_out = _vote(color_probs, buf.confidences, classes['color'],
                          COLOR_CONF)

    # Multi-head path — body / make / model.
    multihead = _load_multihead_model()
    with torch.inference_mode():
        mh_logits = multihead(crops_t)
    mh_probs = {task: torch.softmax(t, dim=1) for task, t in mh_logits.items()}

    body_out = _vote(mh_probs['body'], buf.confidences, classes['body'],
                     BODY_CONF)
    make_out = _vote(mh_probs['make'], buf.confidences, classes['make'],
                     MAKE_CONF)
    # Run the model head on idle tracks too — the original spec deferred
    # this to drive-by-only out of caution, but parked cars give a
    # better-sampled view (multi-angle from the same lane) than a brief
    # drive-by. We'd rather see weak model predictions and tune the
    # threshold from data than not see them at all.
    model_out = _vote(mh_probs['model'], buf.confidences, classes['model'],
                      MODEL_CONF)

    make_out, model_out = _enforce_make_model_consistency(
        make_out, model_out, classes['make_to_models'],
    )

    # Confidence-reporting convention (consistent across all 4 heads):
    #   conf=None  → head was deliberately not run (IR-suppressed color,
    #                idle-event model). Distinguishes "skipped" from
    #                "ran but below threshold" when tuning thresholds.
    #   conf=float → head ran. label is None when the winning class's
    #                weighted confidence was below the threshold; the
    #                conf value still reflects what the vote produced
    #                so the threshold can be tuned from real data.
    # Before this fix, color + model conf were also nulled out when
    # label was None — that hid the losing-confidence info and made
    # it impossible to see "color was 0.53, just under 0.55" vs
    # "color was 0.18, way off".
    color_skipped = is_ir_track
    return {
        'color': color_out[0],
        'color_confidence': None if color_skipped else color_out[1],
        'body_type': body_out[0],
        'body_type_confidence': body_out[1],
        'make': make_out[0],
        'make_confidence': make_out[1],
        'model': model_out[0],
        'model_confidence': model_out[1],
        'voting_samples': len(buf.crops),
        'classifier_version': MODEL_VERSION,
        'ir_track': is_ir_track,
    }
