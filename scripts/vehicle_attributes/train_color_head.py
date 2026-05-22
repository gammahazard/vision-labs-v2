"""Train a ConvNeXt-Tiny color classifier head on VeRi-776.

Reads VeRi-776's `train_label.xml`. The XML's `colorID` is 1-indexed; we map
it to the 0-indexed COLOR_LABELS list so the trained weights drop straight
into `services/vehicle-attributes/classifier.py` without a relabel pass.

The image transform here MUST stay aligned with the inference-time
`_preprocess` in services/vehicle-attributes/classifier.py — train/inference
divergence is the #1 source of "high val acc, bad live predictions" for
this kind of pipeline.

Usage:
    python scripts/vehicle_attributes/train_color_head.py \\
        --data-dir ./datasets/veri776 \\
        --output ./training-output \\
        --epochs 10
"""
import argparse
import json
import logging
import xml.etree.ElementTree as ET
from pathlib import Path

import cv2
import numpy as np
import timm
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, Dataset


logger = logging.getLogger("train_color_head")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")


# VeRi-776 colorID order (1-indexed in the XML) — verified against
# vehiclereid.github.io/VeRi. Mirrors services/vehicle-attributes/classes/
# color_classes.json so the saved head plugs in without label permutation.
COLOR_LABELS = ["yellow", "orange", "green", "gray", "red",
                "blue", "white", "golden", "brown", "black"]

_IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


def _transform(rgb: np.ndarray, train: bool) -> torch.Tensor:
    """Resize-shorter-side-to-224 → center-crop 224 → normalize. Mirror of
    services/vehicle-attributes/classifier.py:_preprocess, plus a random
    horizontal flip in train mode (color labels are flip-invariant).
    """
    h, w = rgb.shape[:2]
    short = min(h, w)
    scale = 224.0 / short
    new_h = max(224, int(round(h * scale)))
    new_w = max(224, int(round(w * scale)))
    rgb = cv2.resize(rgb, (new_w, new_h), interpolation=cv2.INTER_AREA)
    y0 = (new_h - 224) // 2
    x0 = (new_w - 224) // 2
    rgb = rgb[y0:y0 + 224, x0:x0 + 224]
    if train and np.random.random() < 0.5:
        rgb = rgb[:, ::-1, :].copy()
    arr = rgb.astype(np.float32) / 255.0
    arr = (arr - _IMAGENET_MEAN) / _IMAGENET_STD
    return torch.from_numpy(np.transpose(arr, (2, 0, 1)).copy())


class VeRi776ColorDataset(Dataset):
    """Yields (image_tensor, color_label_0_indexed) pairs from VeRi-776.

    Resolves the dataset root tolerantly — Kaggle mirrors sometimes wrap the
    canonical layout in an extra `VeRi/` directory. We search for the XML
    rather than hard-coding a path.
    """

    def __init__(self, data_dir: Path, split: str = "train"):
        self.data_dir = Path(data_dir)
        self.train = (split == "train")

        xml_name = f"{split}_label.xml"
        candidates = list(self.data_dir.rglob(xml_name))
        if not candidates:
            raise FileNotFoundError(
                f"could not find {xml_name} under {self.data_dir} — is the "
                f"VeRi-776 archive fully extracted there?"
            )
        label_xml = candidates[0]
        self.image_root = label_xml.parent / f"image_{split}"
        if not self.image_root.exists():
            raise FileNotFoundError(
                f"image_{split}/ not found next to {label_xml}"
            )

        # VeRi-776's XML declares encoding="gb2312" which Python's stdlib
        # parser rejects. The attribute values we actually read (imageName,
        # colorID, etc.) are pure ASCII, so we re-encode as utf-8 in-memory
        # before parsing.
        raw = label_xml.read_bytes().replace(b'gb2312', b'utf-8', 1)
        root = ET.fromstring(raw)
        items_node = root.find("Items")
        if items_node is None:
            raise ValueError(f"{label_xml} has no <Items> element")

        self.samples: list[tuple[str, int]] = []
        skipped = 0
        for item in items_node.findall("Item"):
            name = item.get("imageName")
            color_id_str = item.get("colorID")
            if not name or not color_id_str:
                skipped += 1
                continue
            try:
                cid = int(color_id_str) - 1
            except ValueError:
                skipped += 1
                continue
            if cid < 0 or cid >= len(COLOR_LABELS):
                skipped += 1
                continue
            self.samples.append((name, cid))

        logger.info(
            f"{split}: loaded {len(self.samples)} samples from {label_xml} "
            f"({skipped} skipped — missing/invalid colorID)"
        )

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, idx: int):
        name, label = self.samples[idx]
        path = self.image_root / name
        bgr = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if bgr is None:
            # One bad image shouldn't kill the worker — return zeros and
            # let cross-entropy treat it as a uniformly-wrong prediction.
            return torch.zeros(3, 224, 224), label
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        return _transform(rgb, train=self.train), label


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path,
                    default=Path("./datasets/veri776"))
    ap.add_argument("--output", type=Path,
                    default=Path("./training-output"))
    ap.add_argument("--epochs", type=int, default=10)
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--num-workers", type=int, default=4)
    ap.add_argument("--lr", type=float, default=1e-3)
    args = ap.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    logger.info(f"device: {device}")

    backbone = timm.create_model(
        "convnext_tiny", pretrained=True, num_classes=0,
    ).to(device)
    backbone.train(False)
    for p in backbone.parameters():
        p.requires_grad = False

    color_head = nn.Linear(backbone.num_features, len(COLOR_LABELS)).to(device)

    train_ds = VeRi776ColorDataset(args.data_dir, split="train")
    train_loader = DataLoader(
        train_ds, batch_size=args.batch_size,
        shuffle=True, num_workers=args.num_workers,
        pin_memory=(device == "cuda"), drop_last=True,
    )
    val_ds = VeRi776ColorDataset(args.data_dir, split="test")
    val_loader = DataLoader(
        val_ds, batch_size=args.batch_size,
        shuffle=False, num_workers=args.num_workers,
        pin_memory=(device == "cuda"),
    )

    opt = torch.optim.AdamW(color_head.parameters(), lr=args.lr)
    criterion = nn.CrossEntropyLoss()

    for epoch in range(args.epochs):
        color_head.train()
        total_loss = 0.0
        total_correct = 0
        n = 0
        for imgs, labels in train_loader:
            imgs = imgs.to(device, non_blocking=True)
            labels = labels.to(device, non_blocking=True)
            with torch.no_grad():
                feats = backbone(imgs)
            logits = color_head(feats)
            loss = criterion(logits, labels)
            opt.zero_grad()
            loss.backward()
            opt.step()
            total_loss += float(loss.item()) * imgs.size(0)
            total_correct += int((logits.argmax(1) == labels).sum().item())
            n += imgs.size(0)

        # Val pass — deterministic transform (train=False on the dataset)
        # so we measure the model in the same regime as inference.
        color_head.train(False)
        val_correct = 0
        val_n = 0
        with torch.no_grad():
            for imgs, labels in val_loader:
                imgs = imgs.to(device, non_blocking=True)
                labels = labels.to(device, non_blocking=True)
                feats = backbone(imgs)
                logits = color_head(feats)
                val_correct += int((logits.argmax(1) == labels).sum().item())
                val_n += imgs.size(0)
        logger.info(
            f"epoch {epoch + 1}/{args.epochs} "
            f"loss={total_loss / n:.4f} "
            f"train_acc={total_correct / n:.4f} "
            f"val_acc={val_correct / val_n:.4f}"
        )

    out_path = args.output / "color_head.pth"
    torch.save({
        "head_state_dict": color_head.state_dict(),
        "backbone_name": "convnext_tiny",
        "num_features": backbone.num_features,
        "color_labels": COLOR_LABELS,
    }, out_path)
    logger.info(f"saved color head to {out_path}")

    classes_out = args.output / "color_classes.json"
    classes_out.write_text(json.dumps(COLOR_LABELS, indent=2))
    logger.info(f"saved color labels to {classes_out}")


if __name__ == "__main__":
    main()
