"""Fine-tune ConvNeXt-Tiny multi-head on Stanford Cars-196.

Loads color head from train_color_head.py output, trains body/make/model
heads jointly on Stanford Cars, exports combined safetensors.

Usage:
    python train_multihead.py \\
        --color-head-checkpoint ./training-output/color_head.pth \\
        --output ./training-output --epochs 15
"""
import argparse
from pathlib import Path

import torch
import torch.nn as nn  # noqa: F401  (used in DataLoader/loss setup below NotImplementedError)
import timm  # noqa: F401  (used after adapter fills in DataLoader)


BODY_KEYWORDS = {
    'sedan': 'sedan',
    'suv': 'suv',
    'coupe': 'coupe',
    'pickup': 'pickup',
    'cab': 'pickup',
    'van': 'van',
    'hatchback': 'hatchback',
    'convertible': 'convertible',
    'wagon': 'wagon',
}
BODY_FALLBACK = 'sedan'


def extract_make(class_name: str) -> str:
    return class_name.split()[0]


def extract_body(class_name: str) -> str:
    low = class_name.lower()
    for kw, label in BODY_KEYWORDS.items():
        if kw in low:
            return label
    return BODY_FALLBACK


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--color-head-checkpoint", type=Path, required=True)
    ap.add_argument("--data-dir", type=Path,
                    default=Path("./datasets/stanford_cars"))
    ap.add_argument("--output", type=Path, default=Path("./training-output"))
    ap.add_argument("--epochs", type=int, default=15)
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--lr", type=float, default=1e-4)
    args = ap.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)
    _ = "cuda" if torch.cuda.is_available() else "cpu"

    raise NotImplementedError(
        "Adapt this script to your local Stanford Cars-196 layout. The "
        "BODY_KEYWORDS + extract_make/extract_body helpers above give you "
        "the static label-derivation logic. You need to plug in your "
        "DataLoader, joint loss function (sum of cross-entropy for body + "
        "make + model — color head stays frozen from step 1), and the "
        "safetensors export of the final combined model. The output should "
        "ALSO regenerate the 5 class JSONs in "
        "services/vehicle-attributes/classes/ to match the actual training "
        "labels (replacing the stubs from the initial commit)."
    )


if __name__ == "__main__":
    main()
