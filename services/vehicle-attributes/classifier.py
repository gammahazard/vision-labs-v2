"""ConvNeXt-Tiny multi-head classifier for vehicle attributes (Phase 3 v0).

Loaded lazily on first inference call so the service can boot quickly +
fail fast if HF Hub is unreachable. Single-process singleton.

Public entry: run_classifier_and_vote(buf, event_kind) -> dict
"""
import logging

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
