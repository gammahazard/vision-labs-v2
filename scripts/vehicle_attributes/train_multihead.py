"""Fine-tune ConvNeXt-Tiny + make/body/model heads on Stanford Cars-196.

Reads `Donghyun99/Stanford-Cars` via the `datasets` library (auto-cached
under ~/.cache/huggingface). Builds per-image make + body labels by
parsing the dataset's class names — Stanford Cars-196 encodes them in
strings like "Honda Accord Sedan 2012".

The image transform here MUST stay aligned with the inference-time
`_preprocess` in services/vehicle-attributes/classifier.py. Train-time
adds RandomResizedCrop + horizontal flip + mild ColorJitter; eval is
deterministic and matches inference exactly.

Backbone is fine-tuned (unlike the color head which froze it) — fine-
grained vehicle make/model discrimination needs car-specific features
that frozen ImageNet pretraining doesn't give.

Usage:
    python scripts/vehicle_attributes/train_multihead.py \\
        --output ./training-output \\
        --epochs 20
"""
import argparse
import json
import logging
import re
from pathlib import Path

import cv2
import numpy as np
import timm
import torch
import torch.nn as nn
import torch.nn.functional as F
from datasets import load_dataset
from torch.utils.data import DataLoader, Dataset


logger = logging.getLogger("train_multihead")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")


# ---------------------------------------------------------------------------
# Label derivation from Stanford Cars-196 class names
# ---------------------------------------------------------------------------

# Multi-word manufacturer prefixes — must match before falling back to the
# single-word default. Order matters only for substring overlaps (none here).
_MULTIWORD_MAKES = (
    "AM General", "Aston Martin", "Land Rover",
    "Rolls-Royce", "Mercedes-Benz",
)

# Body keyword priority — earlier patterns win when multiple match
# (e.g. "Bugatti Veyron 16.4 Convertible" matches both convertible AND
# coupe-style cues elsewhere; convertible wins).
_BODY_PRIORITY = [
    ("convertible", r"\bconvertible\b"),
    ("hatchback",   r"\bhatchback\b"),
    ("wagon",       r"\bwagon\b"),
    ("coupe",       r"\bcoupe\b"),
    ("minivan",     r"\bminivan\b"),
    ("sedan",       r"\bsedan\b"),
    ("suv",         r"\bsuv\b"),
    ("pickup",      r"(cab|pickup)"),  # Cab variants: SuperCab, Crew Cab, etc.
    ("van",         r"\bvan\b"),
]

# Class names that don't carry a body keyword — hand-labeled overrides.
_BODY_OVERRIDES = {
    "Acura Integra Type R 2001":      "coupe",
    "Acura TL Type-S 2008":           "sedan",
    "Buick Regal GS 2012":            "sedan",
    "Chevrolet HHR SS 2010":          "wagon",
    "Chevrolet Cobalt SS 2010":       "coupe",
    "Chevrolet Corvette ZR1 2012":    "coupe",
    "Chevrolet Corvette Ron Fellows Edition Z06 2007": "coupe",
    "Chevrolet TrailBlazer SS 2009":  "suv",
    "Chrysler 300 SRT-8 2010":        "sedan",
    "Dodge Challenger SRT8 2011":     "coupe",
    "Dodge Charger SRT-8 2009":       "sedan",
    "FIAT 500 Abarth 2012":           "hatchback",
    "Ford Ranger SuperCab 2011":      "pickup",
    "Jaguar XK XKR 2012":             "coupe",
    "Lamborghini Gallardo LP 570-4 Superleggera 2012": "coupe",
}


def extract_make(class_name: str) -> str:
    for prefix in _MULTIWORD_MAKES:
        if class_name.startswith(prefix):
            return prefix
    return class_name.split()[0]


def extract_body(class_name: str) -> str:
    if class_name in _BODY_OVERRIDES:
        return _BODY_OVERRIDES[class_name]
    lc = class_name.lower()
    for label, pat in _BODY_PRIORITY:
        if re.search(pat, lc):
            return label
    raise ValueError(
        f"no body keyword found for {class_name!r}; add to _BODY_OVERRIDES"
    )


# ---------------------------------------------------------------------------
# Preprocessing — must mirror services/vehicle-attributes/classifier.py
# ---------------------------------------------------------------------------

_IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


def _transform(rgb: np.ndarray, train: bool) -> torch.Tensor:
    """Resize-shorter-side-to-(224 or random) → 224 crop → normalize.

    Train mode: random crop scale 0.8-1.0 + random flip + mild color jitter.
    Eval mode: deterministic center crop, no augmentation. Mirrors the
    inference-time `_preprocess` in classifier.py exactly.
    """
    h, w = rgb.shape[:2]
    if train:
        scale_factor = 0.8 + 0.2 * np.random.random()  # [0.8, 1.0]
        target_short = int(round(224 / scale_factor))
    else:
        target_short = 224
    short = min(h, w)
    scale = target_short / short
    new_h = max(224, int(round(h * scale)))
    new_w = max(224, int(round(w * scale)))
    rgb = cv2.resize(rgb, (new_w, new_h), interpolation=cv2.INTER_AREA)
    if train:
        y0 = np.random.randint(0, new_h - 223)
        x0 = np.random.randint(0, new_w - 223)
    else:
        y0 = (new_h - 224) // 2
        x0 = (new_w - 224) // 2
    rgb = rgb[y0:y0 + 224, x0:x0 + 224]
    if train and np.random.random() < 0.5:
        rgb = rgb[:, ::-1, :].copy()
    if train:
        # Light color jitter — within ±10% brightness, ±5% per-channel scale.
        rgb = rgb.astype(np.float32)
        rgb *= (0.95 + 0.1 * np.random.random())
        rgb *= (0.95 + 0.1 * np.random.random(3)).astype(np.float32)
        rgb = np.clip(rgb, 0.0, 255.0)
    arr = rgb.astype(np.float32) / 255.0
    arr = (arr - _IMAGENET_MEAN) / _IMAGENET_STD
    return torch.from_numpy(np.transpose(arr, (2, 0, 1)).copy())


# ---------------------------------------------------------------------------
# Dataset wrapper over the HF datasets Dataset
# ---------------------------------------------------------------------------


class StanfordCarsMultiHead(Dataset):
    """Wraps the HF dataset so each item yields
    `(image_tensor, model_label, make_label, body_label)`.

    The model label is the dataset's own 196-way ClassLabel. Make + body
    labels are computed once at construction from the class-name strings.
    """

    def __init__(self, hf_split, class_names: list[str],
                 make_classes: list[str], body_classes: list[str],
                 train: bool):
        self.hf_split = hf_split
        self.train = train
        self._make_id = {m: i for i, m in enumerate(make_classes)}
        self._body_id = {b: i for i, b in enumerate(body_classes)}
        # Per-model-class lookup arrays so __getitem__ doesn't re-parse strings.
        self.make_for_class = [
            self._make_id[extract_make(c)] for c in class_names
        ]
        self.body_for_class = [
            self._body_id[extract_body(c)] for c in class_names
        ]

    def __len__(self) -> int:
        return len(self.hf_split)

    def __getitem__(self, idx: int):
        ex = self.hf_split[idx]
        pil_img = ex["image"]
        model_label = int(ex["label"])
        # PIL → numpy RGB (some Stanford Cars images are grayscale PNGs).
        if pil_img.mode != "RGB":
            pil_img = pil_img.convert("RGB")
        rgb = np.asarray(pil_img)
        img_t = _transform(rgb, train=self.train)
        return img_t, model_label, self.make_for_class[model_label], \
            self.body_for_class[model_label]


# ---------------------------------------------------------------------------
# Model wrapper: backbone + 3 heads
# ---------------------------------------------------------------------------


class MultiHeadModel(nn.Module):
    def __init__(self, num_model: int, num_make: int, num_body: int,
                 backbone_name: str = "convnext_tiny"):
        super().__init__()
        self.backbone = timm.create_model(
            backbone_name, pretrained=True, num_classes=0,
        )
        feat = self.backbone.num_features
        self.model_head = nn.Linear(feat, num_model)
        self.make_head = nn.Linear(feat, num_make)
        self.body_head = nn.Linear(feat, num_body)

    def forward(self, x):
        feats = self.backbone(x)
        return self.model_head(feats), self.make_head(feats), \
            self.body_head(feats)


# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="Donghyun99/Stanford-Cars",
                    help="HF dataset repo id")
    ap.add_argument("--output", type=Path,
                    default=Path("./training-output"))
    ap.add_argument("--epochs", type=int, default=20)
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--num-workers", type=int, default=4)
    ap.add_argument("--head-lr", type=float, default=1e-3)
    ap.add_argument("--backbone-lr", type=float, default=1e-4)
    ap.add_argument("--weight-decay", type=float, default=0.05)
    ap.add_argument("--eval-every", type=int, default=2)
    args = ap.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    logger.info(f"device: {device}")

    # ----- dataset -----
    ds = load_dataset(args.dataset)
    class_names = ds["train"].features["label"].names
    make_classes = sorted({extract_make(c) for c in class_names})
    body_classes = sorted({extract_body(c) for c in class_names})
    logger.info(
        f"loaded dataset: {len(ds['train'])} train, {len(ds['test'])} test"
    )
    logger.info(
        f"classes — model:{len(class_names)} make:{len(make_classes)} "
        f"body:{len(body_classes)}"
    )

    train_ds = StanfordCarsMultiHead(
        ds["train"], class_names, make_classes, body_classes, train=True,
    )
    test_ds = StanfordCarsMultiHead(
        ds["test"], class_names, make_classes, body_classes, train=False,
    )
    train_loader = DataLoader(
        train_ds, batch_size=args.batch_size, shuffle=True,
        num_workers=args.num_workers, pin_memory=(device == "cuda"),
        drop_last=True,
    )
    test_loader = DataLoader(
        test_ds, batch_size=args.batch_size, shuffle=False,
        num_workers=args.num_workers, pin_memory=(device == "cuda"),
    )

    # ----- model -----
    model = MultiHeadModel(
        num_model=len(class_names),
        num_make=len(make_classes),
        num_body=len(body_classes),
    ).to(device)

    # Lower LR on the pretrained backbone, higher on the fresh heads.
    opt = torch.optim.AdamW([
        {"params": model.backbone.parameters(), "lr": args.backbone_lr},
        {"params": list(model.model_head.parameters()) +
                   list(model.make_head.parameters()) +
                   list(model.body_head.parameters()),
         "lr": args.head_lr},
    ], weight_decay=args.weight_decay)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=args.epochs)

    # ----- train -----
    for epoch in range(args.epochs):
        model.train()
        loss_sum = 0.0
        correct = {"model": 0, "make": 0, "body": 0}
        n = 0
        for imgs, m_lbl, mk_lbl, b_lbl in train_loader:
            imgs = imgs.to(device, non_blocking=True)
            m_lbl = m_lbl.to(device, non_blocking=True)
            mk_lbl = mk_lbl.to(device, non_blocking=True)
            b_lbl = b_lbl.to(device, non_blocking=True)

            m_logits, mk_logits, b_logits = model(imgs)
            loss = F.cross_entropy(m_logits, m_lbl) \
                + F.cross_entropy(mk_logits, mk_lbl) \
                + F.cross_entropy(b_logits, b_lbl)

            opt.zero_grad()
            loss.backward()
            opt.step()

            bs = imgs.size(0)
            loss_sum += float(loss.item()) * bs
            correct["model"] += int((m_logits.argmax(1) == m_lbl).sum().item())
            correct["make"] += int((mk_logits.argmax(1) == mk_lbl).sum().item())
            correct["body"] += int((b_logits.argmax(1) == b_lbl).sum().item())
            n += bs

        sched.step()
        logger.info(
            f"epoch {epoch + 1}/{args.epochs} "
            f"loss={loss_sum / n:.4f} "
            f"model_acc={correct['model'] / n:.4f} "
            f"make_acc={correct['make'] / n:.4f} "
            f"body_acc={correct['body'] / n:.4f}"
        )

        # ----- eval -----
        if (epoch + 1) % args.eval_every == 0 or epoch + 1 == args.epochs:
            model.train(False)
            with torch.no_grad():
                ec = {"model": 0, "make": 0, "body": 0}
                en = 0
                for imgs, m_lbl, mk_lbl, b_lbl in test_loader:
                    imgs = imgs.to(device, non_blocking=True)
                    m_lbl = m_lbl.to(device, non_blocking=True)
                    mk_lbl = mk_lbl.to(device, non_blocking=True)
                    b_lbl = b_lbl.to(device, non_blocking=True)
                    m_logits, mk_logits, b_logits = model(imgs)
                    ec["model"] += int(
                        (m_logits.argmax(1) == m_lbl).sum().item()
                    )
                    ec["make"] += int(
                        (mk_logits.argmax(1) == mk_lbl).sum().item()
                    )
                    ec["body"] += int(
                        (b_logits.argmax(1) == b_lbl).sum().item()
                    )
                    en += imgs.size(0)
            logger.info(
                f"  eval — model_acc={ec['model'] / en:.4f} "
                f"make_acc={ec['make'] / en:.4f} "
                f"body_acc={ec['body'] / en:.4f}"
            )

    # ----- save -----
    out_path = args.output / "multihead.pth"
    torch.save({
        "state_dict": model.state_dict(),
        "backbone_name": "convnext_tiny",
        "num_features": model.backbone.num_features,
        "model_classes": list(class_names),
        "make_classes": make_classes,
        "body_classes": body_classes,
    }, out_path)
    logger.info(f"saved multi-head model to {out_path}")

    # Class JSONs (replace the stubs in services/vehicle-attributes/classes/).
    (args.output / "model_classes.json").write_text(
        json.dumps(list(class_names), indent=2)
    )
    (args.output / "make_classes.json").write_text(
        json.dumps(make_classes, indent=2)
    )
    (args.output / "body_classes.json").write_text(
        json.dumps(body_classes, indent=2)
    )

    # make → [models] index, used by run_classifier_and_vote for the
    # make/model consistency check at inference time.
    make_to_models: dict[str, list[str]] = {m: [] for m in make_classes}
    for c in class_names:
        make_to_models[extract_make(c)].append(c)
    (args.output / "make_to_models.json").write_text(
        json.dumps(make_to_models, indent=2)
    )
    logger.info(f"saved 4 class JSONs to {args.output}")


if __name__ == "__main__":
    main()
