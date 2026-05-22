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
# ---------------------------------------------------------------------------

_MODEL = None
_CLASSES = None

MODELS_DIR = os.environ.get("VEHICLE_ATTR_MODELS_DIR", "/models")
HF_REPO = os.environ.get("VEHICLE_ATTR_HF_REPO",
                          "gammahazard/vision-labs-vehicle-attributes")
MODEL_NAME = os.environ.get("VEHICLE_ATTR_MODEL", "convnext_tiny_v0")

COLOR_CONF = float(os.environ.get("COLOR_CONF_THRESHOLD", "0.55"))
BODY_CONF = float(os.environ.get("BODY_CONF_THRESHOLD", "0.55"))
MAKE_CONF = float(os.environ.get("MAKE_CONF_THRESHOLD", "0.55"))
MODEL_CONF = float(os.environ.get("MODEL_CONF_THRESHOLD", "0.65"))

MODEL_VERSION = f"v0-{MODEL_NAME}-2026-05-21"


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


def _build_model_arch():
    """Construct the multi-head ConvNeXt-Tiny architecture (no weights)."""
    import torch.nn as nn
    import timm
    classes = _load_classes()
    backbone = timm.create_model('convnext_tiny', pretrained=False,
                                  num_classes=0)
    feat_dim = backbone.num_features

    class MultiHead(nn.Module):
        def __init__(self):
            super().__init__()
            self.backbone = backbone
            self.color_head = nn.Linear(feat_dim, len(classes['color']))
            self.body_head = nn.Linear(feat_dim, len(classes['body']))
            self.make_head = nn.Linear(feat_dim, len(classes['make']))
            self.model_head = nn.Linear(feat_dim, len(classes['model']))

        def forward(self, x):
            feats = self.backbone(x)
            return {
                'color': self.color_head(feats),
                'body':  self.body_head(feats),
                'make':  self.make_head(feats),
                'model': self.model_head(feats),
            }
    return MultiHead()


def _load_model():
    """Lazy-load the trained multi-head model. Downloads weights on first call.
    Raises if weights can't be obtained.
    """
    global _MODEL
    if _MODEL is not None:
        return _MODEL

    import torch
    from huggingface_hub import hf_hub_download

    target_dir = Path(MODELS_DIR)
    target_dir.mkdir(parents=True, exist_ok=True)
    weights_path = target_dir / f"{MODEL_NAME}.safetensors"

    if not weights_path.exists():
        logger.info(f"downloading {MODEL_NAME} weights from {HF_REPO}")
        downloaded = hf_hub_download(
            repo_id=HF_REPO,
            filename=f"{MODEL_NAME}.safetensors",
            local_dir=str(target_dir),
        )
        if Path(downloaded) != weights_path:
            os.replace(downloaded, weights_path)

    model = _build_model_arch()
    from safetensors.torch import load_file
    state = load_file(str(weights_path))
    model.load_state_dict(state)
    model.train(False)
    if torch.cuda.is_available():
        model = model.cuda()
    _MODEL = model
    logger.info(f"loaded {MODEL_NAME} into {'cuda' if torch.cuda.is_available() else 'cpu'}")
    return _MODEL


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def run_classifier_and_vote(buf, event_kind: str) -> dict:
    """Run the multi-head classifier across all crops in the buffer,
    apply voting + thresholds + make-model consistency, return attributes
    dict for storage.py to merge into metadata.json.
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

    model = _load_model()
    crops_t = _preprocess(buf.crops)
    if torch.cuda.is_available() and crops_t.numel() > 0:
        crops_t = crops_t.cuda()

    with torch.inference_mode():
        logits = model(crops_t)
    probs = {task: torch.softmax(t, dim=1) for task, t in logits.items()}

    color_out = _vote(probs['color'], buf.confidences, classes['color'],
                      COLOR_CONF)
    body_out = _vote(probs['body'], buf.confidences, classes['body'],
                     BODY_CONF)
    make_out = _vote(probs['make'], buf.confidences, classes['make'],
                     MAKE_CONF)
    if event_kind == 'idle':
        model_out = (None, 0.0)
    else:
        model_out = _vote(probs['model'], buf.confidences, classes['model'],
                          MODEL_CONF)

    make_out, model_out = _enforce_make_model_consistency(
        make_out, model_out, classes['make_to_models'],
    )

    return {
        'color': color_out[0],
        'color_confidence': color_out[1],
        'body_type': body_out[0],
        'body_type_confidence': body_out[1],
        'make': make_out[0],
        'make_confidence': make_out[1],
        'model': model_out[0],
        'model_confidence': model_out[1] if model_out[0] is not None else None,
        'voting_samples': len(buf.crops),
        'classifier_version': MODEL_VERSION,
    }
