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
