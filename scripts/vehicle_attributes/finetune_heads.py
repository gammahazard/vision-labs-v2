"""Fine-tune the COLOR classifier head on user-labeled tracks.

Adapted from train_color_head.py but:
  - Reads labeled crops via collect_labels.py instead of VeRi-776 XML
  - Warm-starts the head from current HF-Hub weights (color_head_v0.safetensors)
    so the model doesn't forget VeRi-776 patterns and overfit to a small
    user dataset
  - Lower learning rate (1e-4 default vs from-scratch 1e-3) for the same reason
  - Held-out 20% val set + early stopping if val accuracy stops improving
  - Computes a baseline val acc with the warm-started (= current deployed)
    weights before training, so the caller can refuse to deploy when training
    didn't actually improve things on YOUR labels.

Backbone stays frozen (same as the original training script). Only the linear
head weights move. ~7,690 trainable params.

Body retrain is intentionally NOT in this script. Body lives INSIDE the
multihead.safetensors file alongside the make + model heads — deploying a
new body head means re-emitting a full multihead file with the new body
weights merged in with unchanged make+model weights. That's PR2b.

Output: /models/color_head_<version>.safetensors. Bump the
VEHICLE_ATTR_COLOR_MODEL env var to the new version name and recreate the
va containers — they'll auto-load the new file.

Usage (called by retrain_attributes.py orchestrator):
    python finetune_heads.py --head color \\
        --snapshot-root /data/snapshots/vehicles \\
        --classes-dir /app/classes \\
        --current-weights /models/color_head_v0.safetensors \\
        --output /models/color_head_v1.safetensors
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from collections import Counter
from pathlib import Path

import cv2
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, Dataset

# Make collect_labels importable when this script runs inside the va container
sys.path.insert(0, str(Path(__file__).parent))
from collect_labels import collect, LabeledTrack  # noqa: E402


logger = logging.getLogger("finetune_heads")

_IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


def _transform(rgb: np.ndarray, train: bool) -> torch.Tensor:
    """Same as scripts/vehicle_attributes/train_color_head.py:_transform.
    Must mirror services/vehicle-attributes/classifier.py:_preprocess to
    avoid train/infer divergence."""
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


class _LabeledCropDataset(Dataset):
    """Yields (image_tensor, label_int) for a specific head (color | body)."""

    def __init__(self, tracks: list[LabeledTrack], head: str,
                 class_list: list[str], train: bool):
        self.tracks = tracks
        self.head = head  # "color" | "body_type"
        self.class_list = class_list
        self.train = train

    def __len__(self):
        return len(self.tracks)

    def __getitem__(self, idx: int):
        t = self.tracks[idx]
        label_str = getattr(t, self.head)
        label_idx = self.class_list.index(label_str)
        bgr = cv2.imread(str(t.image_path), cv2.IMREAD_COLOR)
        if bgr is None:
            return torch.zeros(3, 224, 224), label_idx
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        return _transform(rgb, train=self.train), label_idx


def _split_train_val(tracks: list[LabeledTrack], head: str,
                     val_frac: float = 0.2, seed: int = 42
                     ) -> tuple[list[LabeledTrack], list[LabeledTrack]]:
    """Random split, not strict-stratified. With small label counts strict
    stratification would empty some classes in val; random is honest noise."""
    eligible = [t for t in tracks if getattr(t, head) is not None]
    rng = random.Random(seed)
    rng.shuffle(eligible)
    n_val = max(0, int(len(eligible) * val_frac))
    return eligible[n_val:], eligible[:n_val]


def _build_model_for_head(num_classes: int, current_weights: Path | None,
                          device: str):
    """Frozen ConvNeXt-Tiny backbone + linear head. Optionally warm-starts
    from `current_weights` so the head doesn't forget VeRi-776/Stanford-Cars
    knowledge when the user-label dataset is small."""
    import timm
    backbone = timm.create_model("convnext_tiny", pretrained=True,
                                  num_classes=0).to(device)
    backbone.train(False)
    for p in backbone.parameters():
        p.requires_grad = False

    head = nn.Linear(backbone.num_features, num_classes).to(device)

    if current_weights and current_weights.exists():
        from safetensors.torch import load_file
        state = load_file(str(current_weights))
        # color_head_v0.safetensors uses keys like "color_head.weight" /
        # "color_head.bias"; multihead_v0 uses "body_head.weight" etc.
        # We're loading into a bare nn.Linear; strip the prefix.
        head_state = {}
        for k, v in state.items():
            short = k.split(".")[-1]
            if short in ("weight", "bias"):
                head_state[short] = v
        if head_state:
            try:
                head.load_state_dict(head_state, strict=False)
                logger.info(f"warm-started head from {current_weights.name}")
            except Exception as e:
                logger.warning(f"couldn't warm-start head from {current_weights}: {e}")
        else:
            logger.warning(f"no recognized head tensors in {current_weights}")

    return backbone, head


def _evaluate(backbone, head, loader, device) -> tuple[int, int]:
    """Returns (correct, total) on a loader."""
    correct = 0
    total = 0
    with torch.no_grad():
        for imgs, labels in loader:
            imgs = imgs.to(device, non_blocking=True)
            labels = labels.to(device, non_blocking=True)
            feats = backbone(imgs)
            logits = head(feats)
            correct += int((logits.argmax(1) == labels).sum().item())
            total += imgs.size(0)
    return correct, total


def finetune(head_name: str, snapshot_root: Path, classes_dir: Path,
              current_weights: Path | None, output: Path,
              epochs: int = 8, batch_size: int = 16, lr: float = 1e-4,
              val_frac: float = 0.2) -> dict:
    """Fine-tune the color head. Returns a results dict the caller prints.

    head_name is restricted to "color" in PR2 — body lives inside the
    multihead .safetensors and needs a separate merge-aware path (PR2b).
    """
    if head_name != "color":
        return {"head": head_name, "trained": False,
                "error": "only color is supported in PR2; body comes in PR2b"}
    track_attr = "color"
    classes_file = "color_classes.json"

    class_list = json.loads((classes_dir / classes_file).read_text())
    logger.info(f"[{head_name}] {len(class_list)} classes loaded from {classes_file}")

    all_tracks = collect(snapshot_root)
    train_tracks, val_tracks = _split_train_val(all_tracks, track_attr, val_frac)
    if not train_tracks:
        return {"head": head_name, "error": "no labeled tracks for this head",
                "trained": False}

    # Class-imbalance reweighting — invert frequency in train set
    class_counts = Counter(getattr(t, track_attr) for t in train_tracks)
    weights = torch.ones(len(class_list), dtype=torch.float32)
    for i, cls in enumerate(class_list):
        if class_counts[cls] > 0:
            weights[i] = 1.0 / class_counts[cls]
    weights = weights / weights.sum() * len(class_list)  # mean ~ 1.0

    device = "cuda" if torch.cuda.is_available() else "cpu"
    logger.info(f"[{head_name}] device={device}, "
                f"train={len(train_tracks)}, val={len(val_tracks)}")

    backbone, head = _build_model_for_head(
        len(class_list), current_weights, device,
    )
    weights = weights.to(device)

    train_ds = _LabeledCropDataset(train_tracks, track_attr, class_list, train=True)
    val_ds = _LabeledCropDataset(val_tracks, track_attr, class_list, train=False)
    train_loader = DataLoader(train_ds, batch_size=batch_size, shuffle=True,
                               num_workers=0, drop_last=False)
    val_loader = (DataLoader(val_ds, batch_size=batch_size, shuffle=False,
                              num_workers=0) if val_tracks else None)

    # Baseline val acc with the warm-started head — what the currently-deployed
    # head would score on this held-out set. If training never beats this, the
    # caller refuses to deploy.
    head.train(False)
    baseline_correct, baseline_total = (
        _evaluate(backbone, head, val_loader, device) if val_loader else (0, 0)
    )

    opt = torch.optim.AdamW(head.parameters(), lr=lr)
    criterion = nn.CrossEntropyLoss(weight=weights)

    best_val_acc = (baseline_correct / baseline_total) if baseline_total > 0 else None
    best_state = {k: v.detach().clone() for k, v in head.state_dict().items()}
    patience_left = 3
    history = []

    for epoch in range(epochs):
        head.train()
        total_loss = 0.0
        train_correct = 0
        n = 0
        for imgs, labels in train_loader:
            imgs = imgs.to(device, non_blocking=True)
            labels = labels.to(device, non_blocking=True)
            with torch.no_grad():
                feats = backbone(imgs)
            logits = head(feats)
            loss = criterion(logits, labels)
            opt.zero_grad()
            loss.backward()
            opt.step()
            total_loss += float(loss.item()) * imgs.size(0)
            train_correct += int((logits.argmax(1) == labels).sum().item())
            n += imgs.size(0)

        head.train(False)
        if val_loader:
            vc, vt = _evaluate(backbone, head, val_loader, device)
            val_acc = vc / vt if vt > 0 else None
        else:
            val_acc = None

        train_acc = train_correct / n if n > 0 else None
        avg_loss = total_loss / n if n > 0 else float("nan")
        history.append({
            "epoch": epoch + 1, "loss": avg_loss,
            "train_acc": train_acc, "val_acc": val_acc,
        })
        msg = (f"[{head_name}] epoch {epoch+1}/{epochs} "
               f"loss={avg_loss:.4f} train_acc={train_acc:.4f}")
        if val_acc is not None:
            msg += f" val_acc={val_acc:.4f}"
        logger.info(msg)

        if val_acc is not None:
            if best_val_acc is None or val_acc > best_val_acc:
                best_val_acc = val_acc
                best_state = {k: v.detach().clone() for k, v in head.state_dict().items()}
                patience_left = 3
            else:
                patience_left -= 1
                if patience_left <= 0:
                    logger.info(f"[{head_name}] early stop (no val improvement)")
                    break

    head.load_state_dict(best_state)

    # Save with the same key prefix the inference code expects:
    # services/vehicle-attributes/classifier.py loads via load_state_dict
    # on the FULL ColorModel which has self.color_head — so the keys must
    # be "color_head.weight" and "color_head.bias".
    from safetensors.torch import save_file
    out_state = {f"color_head.{k}": v.cpu() for k, v in best_state.items()}
    output.parent.mkdir(parents=True, exist_ok=True)
    save_file(out_state, str(output))
    logger.info(f"[{head_name}] saved {output}")

    return {
        "head": head_name,
        "trained": True,
        "output": str(output),
        "train_count": len(train_tracks),
        "val_count": len(val_tracks),
        "baseline_val_acc": (baseline_correct / baseline_total)
                            if baseline_total > 0 else None,
        "best_val_acc": best_val_acc,
        "history": history,
        "class_distribution": dict(class_counts),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--head", choices=["color"], required=True,
                    help="only 'color' in PR2; body finetune (PR2b) needs a "
                         "multihead-merge path because body lives inside "
                         "multihead.safetensors alongside make + model")
    ap.add_argument("--snapshot-root", type=Path,
                    default=Path("/data/snapshots/vehicles"))
    ap.add_argument("--classes-dir", type=Path, default=Path("/app/classes"))
    ap.add_argument("--current-weights", type=Path, default=None,
                    help="warm-start head from this .safetensors (skip if missing)")
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--epochs", type=int, default=8)
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--val-frac", type=float, default=0.2)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = finetune(
        head_name=args.head, snapshot_root=args.snapshot_root,
        classes_dir=args.classes_dir, current_weights=args.current_weights,
        output=args.output, epochs=args.epochs, batch_size=args.batch_size,
        lr=args.lr, val_frac=args.val_frac,
    )
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
